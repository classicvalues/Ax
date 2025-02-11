#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import logging
from math import ceil
from typing import cast, Optional, Tuple, Type, Union

from ax.core.experiment import Experiment
from ax.core.optimization_config import OptimizationConfig
from ax.core.parameter import ChoiceParameter, ParameterType, RangeParameter
from ax.core.search_space import SearchSpace
from ax.modelbridge.generation_strategy import GenerationStep, GenerationStrategy
from ax.modelbridge.registry import Cont_X_trans, Models, Y_trans
from ax.modelbridge.transforms.base import Transform
from ax.modelbridge.transforms.winsorize import Winsorize
from ax.utils.common.logger import get_logger


logger: logging.Logger = get_logger(__name__)


DEFAULT_BAYESIAN_PARALLELISM = 3
MAX_DISCRETE_COMBINATIONS = 65
SAASBO_INCOMPATIBLE_MESSAGE = (
    "SAASBO is incompatible with {} generation strategy. "
    "Disregarding user input `use_saasbo = True`."
)


def _make_sobol_step(
    num_trials: int = -1,
    min_trials_observed: Optional[int] = None,
    enforce_num_trials: bool = True,
    max_parallelism: Optional[int] = None,
    seed: Optional[int] = None,
    should_deduplicate: bool = False,
) -> GenerationStep:
    """Shortcut for creating a Sobol generation step."""
    return GenerationStep(
        model=Models.SOBOL,
        num_trials=num_trials,
        # NOTE: ceil(-1 / 2) = 0, so this is safe to do when num trials is -1.
        min_trials_observed=min_trials_observed or ceil(num_trials / 2),
        enforce_num_trials=enforce_num_trials,
        max_parallelism=max_parallelism,
        model_kwargs={"deduplicate": True, "seed": seed},
        should_deduplicate=should_deduplicate,
    )


def _make_botorch_step(
    num_trials: int = -1,
    min_trials_observed: Optional[int] = None,
    enforce_num_trials: bool = True,
    max_parallelism: Optional[int] = None,
    model: Models = Models.GPEI,
    winsorize: bool = False,
    winsorization_limits: Optional[Tuple[Optional[float], Optional[float]]] = None,
    should_deduplicate: bool = False,
) -> GenerationStep:
    """Shortcut for creating a BayesOpt generation step."""
    if (winsorize and winsorization_limits is None) or (
        winsorization_limits is not None and not winsorize
    ):
        raise ValueError(  # pragma: no cover
            "To apply winsorization, specify `winsorize=True` and provide the "
            "winsorization limits."
        )
    model_kwargs = None
    if winsorize:
        assert winsorization_limits is not None
        model_kwargs = {
            "transforms": [cast(Type[Transform], Winsorize)] + Cont_X_trans + Y_trans,
            "transform_configs": {
                "Winsorize": {
                    "winsorization_lower": winsorization_limits[0],
                    "winsorization_upper": winsorization_limits[1],
                }
            },
        }
    return GenerationStep(
        model=model,
        num_trials=num_trials,
        # NOTE: ceil(-1 / 2) = 0, so this is safe to do when num trials is -1.
        min_trials_observed=min_trials_observed or ceil(num_trials / 2),
        enforce_num_trials=enforce_num_trials,
        max_parallelism=max_parallelism,
        model_kwargs=model_kwargs,
        should_deduplicate=should_deduplicate,
    )


def _suggest_gp_model(
    search_space: SearchSpace,
    num_trials: Optional[int] = None,
    optimization_config: Optional[OptimizationConfig] = None,
    use_saasbo: bool = False,
) -> Union[None, Models]:
    """Suggest a model based on the search space. None means we use Sobol.

    1. We use Sobol if the number of total iterations in the optimization is
    known in advance and there are fewer distinct points in the search space
    than the known intended number of total iterations.
    2. We use BO_MIXED if there are fewer continuous parameters in the search
    space than the sum of options for the *unordered* choice parameters.
    3. We use MOO if `optimization_config` has multiple objectives and `use_saasbo
    is False`.
    4. We use FULLYBAYESIANMOO if `optimization_config` has multiple objectives
    and `use_saasbo is True`.
    5. If none of the above and `use_saasbo is False`, we use GPEI.
    6. If none of the above and `use_saasbo is True`, we use FULLYBAYESIAN.
    """
    num_continuous_parameters, num_discrete_choices = 0, 0
    num_discrete_combinations, num_possible_points = 1, 1
    all_range_parameters_are_int = True
    for parameter in search_space.parameters.values():
        if isinstance(parameter, ChoiceParameter):
            num_discrete_choices += len(parameter.values)
            num_discrete_combinations *= len(parameter.values)
            num_possible_points *= len(parameter.values)
        elif isinstance(parameter, RangeParameter):
            num_continuous_parameters += 1
            if parameter.parameter_type != ParameterType.INT:
                all_range_parameters_are_int = False
            else:
                num_possible_points *= int(parameter.upper - parameter.lower)

    if (  # If number of trials is known and it enough to try all possible points,
        num_trials is not None  # we should use Sobol and not BO.
        and all_range_parameters_are_int
        and num_possible_points <= num_trials
    ):
        logger.info("Using Sobol since we can enumerate the search space.")
        if use_saasbo:
            logger.warn(SAASBO_INCOMPATIBLE_MESSAGE.format("Sobol"))
        return None

    is_moo_problem = optimization_config and optimization_config.is_moo_problem
    if num_continuous_parameters > num_discrete_choices:
        logger.info(
            "Using Bayesian optimization since there are more continuous "
            "parameters than there are categories for the unordered categorical "
            "parameters."
        )
        if is_moo_problem and use_saasbo:
            return Models.FULLYBAYESIANMOO
        elif is_moo_problem and not use_saasbo:
            return Models.MOO
        elif use_saasbo:
            return Models.FULLYBAYESIAN
        else:
            return Models.GPEI
    elif not is_moo_problem and num_discrete_combinations <= MAX_DISCRETE_COMBINATIONS:
        logger.info(
            "Using Bayesian optimization with a categorical kernel for improved "
            "performance with a large number of unordered categorical parameters."
        )
        if use_saasbo:
            logger.warn(SAASBO_INCOMPATIBLE_MESSAGE.format("`BO_MIXED`"))
        return Models.BO_MIXED
    else:
        logger.info(
            f"Using Sobol since there are more than {MAX_DISCRETE_COMBINATIONS} "
            "combinations for the categorical parameters. Consider removing a few "
            "categorical parameters for improved performance. If possible, turn "
            "all ordered categorical variables into RangeParameters"
        )
        if use_saasbo:
            logger.warn(SAASBO_INCOMPATIBLE_MESSAGE.format("Sobol"))

        return None


def choose_generation_strategy(
    search_space: SearchSpace,
    use_batch_trials: bool = False,
    enforce_sequential_optimization: bool = True,
    random_seed: Optional[int] = None,
    winsorize_botorch_model: bool = False,
    winsorization_limits: Optional[Tuple[Optional[float], Optional[float]]] = None,
    no_bayesian_optimization: bool = False,
    num_trials: Optional[int] = None,
    num_initialization_trials: Optional[int] = None,
    max_parallelism_cap: Optional[int] = None,
    max_parallelism_override: Optional[int] = None,
    optimization_config: Optional[OptimizationConfig] = None,
    should_deduplicate: bool = False,
    use_saasbo: bool = False,
    experiment: Optional[Experiment] = None,
) -> GenerationStrategy:
    """Select an appropriate generation strategy based on the properties of
    the search space and expected settings of the experiment, such as number of
    arms per trial, optimization algorithm settings, expected number of trials
    in the experiment, etc.

    Args:
        search_space: SearchSpace, based on the properties of which to select the
            generation strategy.
        use_batch_trials: Whether this generation strategy will be used to generate
            batched trials instead of 1-arm trials.
        enforce_sequential_optimization: Whether to enforce that 1) the generation
            strategy needs to be updated with `min_trials_observed` observations for
            a given generation step before proceeding to the next one and 2) maximum
            number of trials running at once (max_parallelism) if enforced for the
            BayesOpt step. NOTE: `max_parallelism_override` and `max_parallelism_cap`
            settings will still take their effect on max parallelism even if
            `enforce_sequential_optimization=False`, so if those settings are specified,
            max parallelism will be enforced.
        random_seed: Fixed random seed for the Sobol generator.
        winsorize_botorch_model: Whether to apply the winsorization transform
            prior to applying other transforms for fitting the BoTorch model.
        winsorization_limits: Bounds for winsorization, if winsorizing, expressed
            as percentile. Usually only the upper winsorization trim is used when
            minimizing, and only the lower when maximizing.
        no_bayesian_optimization: If True, Bayesian optimization generation
            strategy will not be suggested and quasi-random strategy will be used.
        num_trials: Total number of trials in the optimization, if
            known in advance.
        num_initialization_trials: Specific number of initialization trials, if wanted.
            Typically, initialization trials are generated quasi-randomly.
        max_parallelism_override: Integer, with which to override the default max
            parallelism setting for all steps in the generation strategy returned from
            this function. Each generation step has a `max_parallelism` value, which
            restricts how many trials can run simultaneously during a given generation
            step. By default, the parallelism setting is chosen as appropriate for the
            model in a given generation step. If `max_parallelism_override` is -1,
            no max parallelism will be enforced for any step of the generation strategy.
            Be aware that parallelism is limited to improve performance of Bayesian
            optimization, so only disable its limiting if necessary.
        max_parallelism_cap: Integer cap on parallelism in this generation strategy.
            If specified, `max_parallelism` setting in each generation step will be set
            to the minimum of the default setting for that step and the value of this
            cap. `max_parallelism_cap` is meant to just be a hard limit on parallelism
            (e.g. to avoid overloading machine(s) that evaluate the experiment trials).
            Specify only if not specifying `max_parallelism_override`.
        use_saasbo: Whether to use SAAS prior for any GPEI generation steps.
        experiment: If specified, `_experiment` attribute of the generation strategy
            will be set to this experiment (useful for associating a generation
            strategy with a given experiment before it's first used to ``gen`` with
            that experiment).
    """
    suggested_model = _suggest_gp_model(
        search_space=search_space,
        num_trials=num_trials,
        optimization_config=optimization_config,
        use_saasbo=use_saasbo,
    )
    if not no_bayesian_optimization and suggested_model is not None:
        if not enforce_sequential_optimization and (  # pragma: no cover
            max_parallelism_override or max_parallelism_cap
        ):
            logger.info(
                "If `enforce_sequential_optimization` is False, max parallelism is "
                "not enforced and other max parallelism settings will be ignored."
            )
        if max_parallelism_override and max_parallelism_cap:
            raise ValueError(
                "If `max_parallelism_override` specified, cannot also apply "
                "`max_parallelism_cap`."
            )

        # If number of initialization trials is not specified, estimate it.
        if num_initialization_trials is None:
            if use_batch_trials:  # Batched trials.
                num_initialization_trials = 1
            else:  # 1-arm trials.
                num_initialization_trials = max(5, len(search_space.parameters))

        # Determine max parallelism for the generation steps.
        if max_parallelism_override == -1:
            # `max_parallelism_override` of -1 means no max parallelism enforcement in
            # the generation strategy, which means `max_parallelism=None` in gen. steps.
            sobol_parallelism = bo_parallelism = None
        elif max_parallelism_override is not None:
            sobol_parallelism = bo_parallelism = max_parallelism_override
        elif max_parallelism_cap is not None:  # Max parallelism override is None by now
            sobol_parallelism = max_parallelism_cap
            bo_parallelism = min(max_parallelism_cap, DEFAULT_BAYESIAN_PARALLELISM)
        elif not enforce_sequential_optimization:
            # If no max parallelism settings specified and not enforcing sequential
            # optimization, do not limit parallelism.
            sobol_parallelism = bo_parallelism = None
        else:  # No additional max parallelism settings, use defaults
            sobol_parallelism = None  # No restriction on Sobol phase
            bo_parallelism = DEFAULT_BAYESIAN_PARALLELISM

        gs = GenerationStrategy(
            steps=[
                _make_sobol_step(
                    num_trials=num_initialization_trials,
                    enforce_num_trials=enforce_sequential_optimization,
                    seed=random_seed,
                    max_parallelism=sobol_parallelism,
                    should_deduplicate=should_deduplicate,
                ),
                _make_botorch_step(
                    model=suggested_model,
                    winsorize=winsorize_botorch_model,
                    winsorization_limits=winsorization_limits,
                    max_parallelism=bo_parallelism,
                    should_deduplicate=should_deduplicate,
                ),
            ]
        )
        logger.info(
            f"Using Bayesian Optimization generation strategy: {gs}. Iterations after"
            f" {num_initialization_trials} will take longer to generate due to "
            " model-fitting."
        )
    else:
        gs = GenerationStrategy(
            steps=[
                _make_sobol_step(
                    seed=random_seed, should_deduplicate=should_deduplicate
                )
            ]
        )
        logger.info("Using Sobol generation strategy.")
    if experiment:
        gs.experiment = experiment
    return gs
