from collections import defaultdict
from typing import Callable, Optional, Union

import numpy as np
import pandas as pd

from .exceptions import NotFittedError, InvalidConfigurationError
from .globals import get_rmse
from .learner import Learner, LearnerID
from .strategies import get_strategy
from .strategies.base import RoverStrategy
from .synthesizer import metrics_to_weights


class Rover:

    def __init__(
        self,
        model_type: str,
        y: str,
        col_fixed: dict[str, list[str]],
        col_explore: list[str],
        explore_param: str,
        param_specs: Optional[dict[str, dict]] = None,
        offset: str = "offset",
        weights: str = "weights",
        holdout_cols: Optional[list[str]] = None,
        model_eval_metric: Callable = get_rmse,
    ) -> None:
        """
        Initialization of the Rover class.

        :param model_type: str, the class of regmod model to fit
        :param y: str, the name of the column representing observations
        :param col_fixed: a dict with string keys representing parameters, and values of
            string lists representing columns mapped to the given parameter. Fixed cols
            are not explored over and are present in every learner
        :param col_explore: a list representing the covariate columns rover will explore over
        :param explore_param: str, the parameter we are exploring over
        :param param_specs: TODO - add descriptions for param specs, offset, weights
        :param offset:
        :param weights:
        :param holdout_cols: a list of column names containing 1's and 0's that represent
            folds in the rover cross-validation algorithm
        :param model_eval_metric: a callable used to evaluate cross-validated performance of
            sublearners in rover.
        """
        # parse param_specs
        if param_specs is None:
            param_specs = {}

        self.model_type = model_type
        self.y = y
        self.col_fixed = col_fixed
        self.col_explore = col_explore
        self.explore_param = explore_param
        self.param_specs = param_specs
        self.offset = offset
        self.weights = weights
        self.holdout_cols = holdout_cols
        self.model_eval_metric = model_eval_metric

        self.learners: dict[LearnerID, Learner] = {}

        self.super_learner = None

        # TODO: validate the inputs
        # ...

    @property
    def performances(self) -> dict[tuple[int, ...], Learner]:
        """Convenience property to select only the performance of each learner."""
        return {lid: learner.performance for lid, learner in self.learners.items()}

    @property
    def num_covariates(self) -> int:
        return len(self.all_covariates)

    @property
    def all_covariates(self) -> list[str]:
        """Lazily unpack the provided fixed and explore covariates.

        Create an ordered list of all covariates represented in the dataset.

        Assumption: No duplicates within or without parameter groups
        """
        if not hasattr(self, "_all_covariates"):
            all_covariates = []
            for fixed_cols in self.col_fixed.values():
                all_covariates.extend(fixed_cols)

            all_covariates.extend(self.col_explore)
            self._all_covariates = all_covariates

        return self._all_covariates

    @property
    def default_param_specs(self) -> dict:
        """All fixed columns should be represented in every learner, so we can create
        a default mapping of columns that can be simply parameterized in get_learner
        with the variable explore columns."""
        param_specs = defaultdict(lambda: defaultdict(list))

        for param_name, fixed_columns in self.col_fixed.items():
            param_specs[param_name]["variables"].extend(fixed_columns)
            param_specs[param_name].update(
                self.param_specs.get(param_name, {})
            )
        return param_specs


    def check_is_fitted(self):
        if not self.super_learner:
            raise NotFittedError("Rover has not been ensembled yet")

    def get_learner(self, learner_id: LearnerID, use_cache: bool = True) -> Learner:

        # See if we've already initialized one
        if learner_id in self.learners and use_cache:
            return self.learners[learner_id]

        explore_columns = [self.col_explore[i] for i in learner_id]
        param_specs = self.default_param_specs.copy()

        # Important note: The order is important here since it determines the
        # coefficient value mapping later.
        # Explore columns are added at the end, so that there is a consistent offset
        param_specs[self.explore_param]['variables'] = \
            param_specs[self.explore_param]['variables'] + explore_columns

        return Learner(
            self.model_type,
            self.y,
            param_specs,
            all_covariates=self.all_covariates,
            offset=self.offset,
            weights=self.weights,
            model_eval_metric=self.model_eval_metric,
        )

    def fit(
        self,
        dataset: pd.DataFrame,
        strategy: Union[str, RoverStrategy],
        max_num_models: int = 10,
        kernel_param: float = 10.,
        ratio_cutoff: float = .99
    ) -> None:
        """
        Fits the ensembled super learner.

        Explores over all covariate slices as defined by the input strategy, and fits the
        sublearners.

        The superlearner coefficients are determined by the ensemble method parameters, and the
        super learner itself will be created - to be used in prediction and summarization.

        :param dataset: the dataset to fit individual learners on
        :param strategy: the selection strategy to determine the model tree
        :param max_num_models: the maximum number of models to consider for ensembling
        :param kernel_param: the kernel parameter used to determine bias in ensemble weights
        :param ratio_cutoff: the cross-validated performance score necessary for a learner to
            be considered in ensembling
        :return: None
        """
        self._explore(
            dataset=dataset,
            strategy=strategy
        )
        super_learner = self._create_super_learner(
            max_num_models=max_num_models,
            kernel_param=kernel_param,
            ratio_cutoff=ratio_cutoff
        )
        self.super_learner = super_learner

    def _explore(self, dataset: pd.DataFrame, strategy: Union[str, RoverStrategy]):
        """Explore the entire tree of learners.

        Params:
        dataset: The dataset to fit models on
        strategy: a string or a roverstrategy object. Dictates how the next set of models
            is selected
        """
        if isinstance(strategy, str):
            # If a string is provided, select a strategy for the user.
            strategy_class = get_strategy(strategy)
            strategy = strategy_class(
                num_covs=self.num_covariates,
            )

        curr_ids = {strategy.base_learner_id}

        while curr_ids:
            curr_learners = []
            for learner_id in curr_ids:
                learner = self.get_learner(learner_id)

                if not learner.has_been_fit:
                    curr_learners.append((learner_id, learner))

            self._fit_layer(dataset, curr_learners)
            next_ids = strategy.get_next_layer(
                curr_layer=curr_ids,
                learners=self.learners
            )
            curr_ids = set(next_ids)
        return

    def _fit_layer(
            self,
            dataset: pd.DataFrame,
            learners: list[tuple[tuple[int, ...], Learner]]
    ) -> None:
        """Fit a layer of models. Store results on the learners dict."""
        for learner_id, learner in learners:
            learner.fit(dataset, self.holdout_cols)
            self.learners[learner_id] = learner

    def _generate_ensemble_coefficients(
        self,
        max_num_models: int,
        kernel_param: float,
        ratio_cutoff: float,
    ) -> np.ndarray:
        """
        Generates the weighted ensembled coefficients across all fitted learners.

        :param max_num_models: The maximum number of learners to consider for our weights
        :param kernel_param: The kernel parameter, amount with which to bias towards strongest
            performances
        :param ratio_cutoff: The performance floor which learners must exceed to be considered
            in the ensemble weights
        :return: A vector of weighted coefficients aggregated from sufficiently-performing
            sublearners.
        """

        # Validate the parameters
        if ratio_cutoff > 1 or max_num_models < 1:
            raise InvalidConfigurationError(
                "The ratio cutoff parameter must be < 1, and max_num_models >= 1, "
                "otherwise no models will be used for ensembling."
            )

        learner_ids, coefficients = self._generate_coefficients_matrix()

        # Create weights
        performances = np.array([self.performances[key] for key in learner_ids])
        weights = metrics_to_weights(
            metrics=performances,
            max_num_models=max_num_models,
            kernel_param=kernel_param,
            ratio_cutoff=ratio_cutoff
        )
        # Multiply by the coefficients
        means = coefficients.T.dot(weights)

        return means

    def _generate_coefficients_matrix(self) -> tuple[list[LearnerID], np.ndarray]:
        """Create the full matrix of learner ids, mapped to its relative coefficients.

        Will result in a m x n matrix, where m = the number of fitted learners in rover
        and n is the total number of covariates provided.

        Each cell is the i-th learner's coefficient for the j-th covariate, defaulting to 0
        if the coefficient is not represented in that learner.

        :return:
        """
        learner_ids: list[LearnerID] = []
        performances: np.ndarray = np.array([])
        for learner_id, learner in self.learners.items():

            if learner.performance and learner.opt_coefs is not None:
                learner_ids.append(learner_id)
                performances = np.append(performances, learner.performance)

        # Aggregate the coefficients
        x_dim, y_dim = len(learner_ids), self.num_covariates
        coefficients = np.empty((x_dim, y_dim))

        # Note: 3 methods were profiled for stacking these rows together
        #   a) initialize an empty array and assign rows iteratively
        #   b) use np.vstack to concatenate rows iteratively
        #   c) use np.concatenate to concatenate all rows together

        # Of the 3, initializing the array first seems to be most performant

        for row, learner_id in enumerate(learner_ids):
            opt_coefs = self.learners[learner_id].opt_coefs
            all_coefs = self._learner_coefs_to_global_coefs(learner_id, opt_coefs)
            coefficients[row] = all_coefs

        return learner_ids, coefficients

    def _learner_coefs_to_global_coefs(
        self, learner_id: tuple, opt_coefs: np.array
    ) -> np.array:
        """Given a set of coefficients from a specific learner, map those coefficients to
        the complete set of covariates.

        Ex. we have 5 covariates to explore over, a-e, and set a + b as fixed covariates
        Take a sample learner the fixed covariates + d.

        The resultant coefficients will be length 3, representing a, b, and d. c and e have
        implicit coefficients of size 0 in the global model.

        Taking some example coefficients from this sublearner of [.1, .2, .4],
        this function will return [.1, .2, 0, .4, 0]
        """
        # Since explore cols are appended on after the fixed columns,
        # we'll need to separate out the fixed and explore columns
        offset = self.num_covariates - len(self.col_explore)
        row = np.zeros(self.num_covariates)
        row[:offset] = opt_coefs[:offset]
        explore_indices = np.array(learner_id) + offset
        row[explore_indices] = opt_coefs[offset:]
        return row

    def _create_super_learner(
        self,
        max_num_models: int,
        kernel_param: float,
        ratio_cutoff: float
    ) -> Learner:
        """Call at the end of fit, so model is configured at the end of fit."""
        means = self._generate_ensemble_coefficients(
            max_num_models=max_num_models,
            kernel_param=kernel_param,
            ratio_cutoff=ratio_cutoff
        )
        master_learner_id = tuple(range(self.num_covariates))
        learner = self.get_learner(
            learner_id=master_learner_id,
            use_cache=False
        )
        learner.opt_coefs = means
        return learner

    def predict(self, dataset):
        self.check_is_fitted()
        return self.super_learner.predict(dataset)

    def summary(self):
        NotImplemented
