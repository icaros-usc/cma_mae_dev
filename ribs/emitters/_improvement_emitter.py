"""Provides the ImprovementEmitter."""
import itertools

import numpy as np

from ribs.archives import AddStatus
from ribs.emitters._emitter_base import EmitterBase
from ribs.emitters.opt import CMAEvolutionStrategy


class ImprovementEmitter(EmitterBase):
    """Adapts a covariance matrix towards changes in the archive.

    This emitter originates in `Fontaine 2020
    <https://arxiv.org/abs/1912.02400>`_. Initially, it starts at ``x0`` and
    uses CMA-ES to search for solutions that improve the archive, i.e. solutions
    that add new entries to the archive or improve existing entries. Once CMA-ES
    restarts (see ``restart_rule``), the emitter starts from a randomly chosen
    elite in the archive and continues searching for solutions that improve the
    archive.

    Args:
        archive (ribs.archives.ArchiveBase): An archive to use when creating and
            inserting solutions. For instance, this can be
            :class:`ribs.archives.GridArchive`.
        x0 (np.ndarray): Initial solution.
        sigma0 (float): Initial step size.
        selection_rule ("mu" or "filter"): Method for selecting solutions in
            CMA-ES. With "mu" selection, the first half of the solutions will be
            selected, while in "filter", any solutions that were added to the
            archive will be selected.
        restart_rule ("no_improvement" or "basic"): Method to use when checking
            for restart. With "basic", only the default CMA-ES convergence rules
            will be used, while with "no_improvement", the emitter will restart
            when none of the proposed solutions were added to the archive.
        weight_rule ("truncation" or "active"): Method for generating weights in
            CMA-ES. Either "truncation" (positive weights only) or "active"
            (include negative weights).
        bounds (None or array-like): Bounds of the solution space. Solutions are
            clipped to these bounds. Pass None to indicate there are no bounds.
            Alternatively, pass an array-like to specify the bounds for each
            dim. Each element in this array-like can be None to indicate no
            bound, or a tuple of ``(lower_bound, upper_bound)``, where
            ``lower_bound`` or ``upper_bound`` may be None to indicate no bound.
        batch_size (int): Number of solutions to return in :meth:`ask`. If not
            passed in, a batch size will automatically be calculated.
        seed (int): Value to seed the random number generator. Set to None to
            avoid a fixed seed.
    Raises:
        ValueError: If any of ``selection_rule``, ``restart_rule``, or
            ``weight_rule`` is invalid.
    """

    def __init__(self,
                 archive,
                 x0,
                 sigma0,
                 selection_rule="filter",
                 restart_rule="no_improvement",
                 weight_rule="truncation",
                 bounds=None,
                 batch_size=None,
                 seed=None):
        self._rng = np.random.default_rng(seed)
        self._batch_size = batch_size
        self._x0 = np.array(x0, dtype=archive.dtype)
        self._sigma0 = sigma0
        EmitterBase.__init__(
            self,
            archive,
            len(self._x0),
            bounds,
        )

        if selection_rule not in ["mu", "filter"]:
            raise ValueError(f"Invalid selection_rule {selection_rule}")
        self._selection_rule = selection_rule

        if restart_rule not in ["basic", "no_improvement"]:
            raise ValueError(f"Invalid restart_rule {restart_rule}")
        self._restart_rule = restart_rule

        opt_seed = None if seed is None else self._rng.integers(10_000)
        self.opt = CMAEvolutionStrategy(sigma0, batch_size, self._solution_dim,
                                        weight_rule, opt_seed,
                                        self.archive.dtype)
        self.opt.reset(self._x0)
        self._num_parents = (self.opt.batch_size //
                             2 if selection_rule == "mu" else None)
        self._batch_size = self.opt.batch_size
        self._restarts = 0  # Currently not exposed publicly.

    @property
    def x0(self):
        """numpy.ndarray: Initial solution for the optimizer."""
        return self._x0

    @property
    def sigma0(self):
        """float: Initial step size for the CMA-ES optimizer."""
        return self._sigma0

    @property
    def batch_size(self):
        """int: Number of solutions to return in :meth:`ask`."""
        return self._batch_size

    def ask(self, grad_estimate=False):
        """Samples new solutions from a multivariate Gaussian.

        The multivariate Gaussian is parameterized by the CMA-ES optimizer.

        Returns:
            ``(batch_size, solution_dim)`` array -- contains ``batch_size`` new
            solutions to evaluate.
        """
        return self.opt.ask(self.lower_bounds, self.upper_bounds)

    def _check_restart(self, num_parents):
        """Emitter-side checks for restarting the optimizer.

        The optimizer also has its own checks.
        """
        if self._restart_rule == "no_improvement":
            return num_parents == 0
        return False

    def tell(self, solutions, objective_values, behavior_values, jacobian=None, metadata=None):
        """Gives the emitter results from evaluating solutions.

        As solutions are inserted into the archive, we record their "improvement
        value" -- conveniently, this is the ``value`` returned by
        :meth:`ribs.archives.ArchiveBase.add`. We then rank the solutions
        according to their add status (new solutions rank in front of
        solutions that improved existing entries in the archive, which rank
        ahead of solutions that were not added), followed by their improvement
        value.  We then pass the ranked solutions to the underlying CMA-ES
        optimizer to update the search parameters.

        Args:
            solutions (numpy.ndarray): Array of solutions generated by this
                emitter's :meth:`ask()` method.
            objective_values (numpy.ndarray): 1D array containing the objective
                function value of each solution.
            behavior_values (numpy.ndarray): ``(n, <behavior space dimension>)``
                array with the behavior space coordinates of each solution.
            jacobian (numpy.ndarray): Ignored for QD algorithms.
            metadata (numpy.ndarray): 1D object array containing a metadata
                object for each solution.
        """
        ranking_data = []
        new_sols = 0
        metadata = itertools.repeat(None) if metadata is None else metadata
        for i, (sol, obj, beh, meta) in enumerate(
                zip(solutions, objective_values, behavior_values, metadata)):
            status, value, _ = self.archive.add(sol, obj, beh, meta)
            ranking_data.append((status, value, i))
            if status in (AddStatus.NEW, AddStatus.IMPROVE_EXISTING):
                new_sols += 1
        # New solutions sort ahead of improved ones, which sort ahead of ones
        # that were not added.
        ranking_data.sort(reverse=True)
        indices = [d[2] for d in ranking_data]

        num_parents = (new_sols if self._selection_rule == "filter" else
                       self._num_parents)

        self.opt.tell(solutions[indices], num_parents)

        # Check for reset.
        if (self.opt.check_stop([value for status, value, i in ranking_data]) or
                self._check_restart(new_sols)):
            new_x0 = self.archive.get_random_elite()[0]
            self.opt.reset(new_x0)
            self._restarts += 1
