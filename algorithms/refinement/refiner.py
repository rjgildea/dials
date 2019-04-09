#!/usr/bin/env python
#
# dials.algorithms.refinement.refiner.py
#
#  Copyright (C) 2013 Diamond Light Source and STFC Rutherford Appleton
#                     Laboratory, UK.
#
#  Authors: James Parkhurst, David Waterman
#
#  This code is distributed under the BSD license, a copy of which is
#  included in the root directory of this package.

"""Refiner is the refinement module public interface. RefinerFactory is
what should usually be used to construct a Refiner."""

from __future__ import absolute_import, division
from __future__ import print_function
import copy
import logging

logger = logging.getLogger(__name__)

from dxtbx.model.experiment_list import ExperimentList
from dials.array_family import flex
from dials.algorithms.refinement.refinement_helpers import ordinal_number
from libtbx.phil import parse
from dials.util import Sorry
import libtbx

# The include scope directive does not work here. For example:
#
#   include scope dials.algorithms.refinement.outlier_detection.phil_scope
#
# results in:
#
#   AttributeError: 'module' object has no attribute 'refinement'
#
# to work around this, just include external phil scopes as strings
from dials.algorithms.refinement.reflection_manager import (
    phil_str as reflections_phil_str,
)
from dials.algorithms.refinement.target import phil_str as target_phil_str
from dials.algorithms.refinement.parameterisation import (
    phil_str as parameterisation_phil_str,
)
from dials.algorithms.refinement.engine import refinery_phil_str

format_data = {
    "reflections_phil": reflections_phil_str,
    "target_phil": target_phil_str,
    "parameterisation_phil": parameterisation_phil_str,
    "refinery_phil": refinery_phil_str,
}
phil_scope = parse(
    """
refinement
  .help = "Parameters to configure the refinement"
{

  mp
    .expert_level = 2
  {
    nproc = 1
      .type = int(value_min=1)
      .help = "The number of processes to use. Not all choices of refinement"
              "engine support nproc > 1. Where multiprocessing is possible,"
              "it is helpful only in certain circumstances, so this is not"
              "recommended for typical use."
  }

  verbosity = 0
    .help = "verbosity level"
    .type = int(value_min=0)
    .expert_level = 1

  parameterisation
    .help = "Parameters to control the parameterisation of experimental models"
  {
    %(parameterisation_phil)s
  }

  %(refinery_phil)s

  target
    .help = "Parameters to configure the target function"
    .expert_level = 1
  {
    %(target_phil)s
  }

  reflections
    .help = "Parameters used by the reflection manager"
  {
    %(reflections_phil)s
  }

}
"""
    % format_data,
    process_includes=True,
)


def _copy_experiments_for_refining(experiments):
    """
    Make a partial copy of experiments, copying properties used in refinement.

    Any experiment property that can be altered by refinement e.g. Beam,
    Detector and Goniometer will be deep-copied, whereas anything that the
    refiner doesn't touch (e.g. Scan, ImageSet) will be left as original
    references.

    This makes it safe to pass an object into the refiner or get an object
    out of the refiner without having to worry about your copy being
    unexpectedly altered, but saves time by avoiding the copying of potentially
    expensive experiment properties (e.g. ImageSet and its attributes).

    Args
      experiments (Experiment or ExperimentList or Iterable[Experiment]):

    Returns:
      ExperimentList: The copied experiments in new ExperimentList
    """
    # Look for a non-list e.g. a single experiment and convert to a list
    if not hasattr(experiments, "__iter__"):
        experiments = [experiments]
    out_list = ExperimentList()
    # Save a map of object id to copies so shared objects remain shared
    id_memo = {}

    # Copy each experiment individually
    for experiment in experiments:
        # Be inclusive about the initial copy
        new_exp = copy.copy(experiment)

        # Ensure every 'refined' attribute is uniquely copied
        for model in ["beam", "goniometer", "detector", "crystal"]:
            original = getattr(experiment, model)
            if id(original) not in id_memo:
                id_memo[id(original)] = copy.deepcopy(original)
            # assign the new copy to the experiment
            setattr(new_exp, model, id_memo[id(original)])

        # Collect this together
        out_list.append(new_exp)

    return out_list


class RefinerFactory(object):
    """Factory class to create refiners"""

    @classmethod
    def _filter_reflections(cls, reflections):
        """Return a copy of the input reflections filtered to keep only
        those columns that are required by refinement"""

        cols = [
            "id",
            "miller_index",
            "panel",
            "s1",
            "xyzobs.mm.value",
            "xyzobs.px.value",
            "xyzcal.px",
            "xyzobs.mm.variance",
            "flags",
            "delpsical.weights",
        ]
        # NB xyzobs.px.value & xyzcal.px required by SauterPoon outlier rejector
        # NB delpsical.weights is used by ExternalDelPsiWeightingStrategy
        rt = flex.reflection_table()

        # copy columns to the new table. Could use the select method
        # for this except that 's1' is optional in the input so would want
        # to copy that in like this if present anyway
        for k in cols:
            if k in reflections.keys():
                rt[k] = reflections[k]

        return rt

    @classmethod
    def from_parameters_data_experiments(
        cls, params, reflections, experiments, verbosity=None
    ):

        # TODO Checks on the input
        # E.g. does every experiment contain at least one overlapping model with at
        # least one other experiment? Are all the experiments either rotation series
        # or stills (the combination of both not yet supported)?

        # if no verbosity override is given, take from the parameters
        if verbosity is None:
            verbosity = params.refinement.verbosity

        # copy the experiments
        experiments = _copy_experiments_for_refining(experiments)

        # copy and filter the reflections
        reflections = cls._filter_reflections(reflections)

        return cls._build_components(
            params, reflections, experiments, verbosity=verbosity
        )

    @classmethod
    def _build_components(cls, params, reflections, experiments, verbosity):
        """low level build"""

        if verbosity == 0:
            logging.getLogger("dials.algorithms.refinement").setLevel(logging.WARNING)

        # Currently a refinement job can only have one parameterisation of the
        # prediction equation. This can either be of the XYDelPsi (stills) type, the
        # XYPhi (scans) type or the scan-varying XYPhi type with a varying crystal
        # model
        single_as_still = params.refinement.parameterisation.treat_single_image_as_still
        exps_are_stills = []
        for exp in experiments:
            if exp.scan is None:
                exps_are_stills.append(True)
            elif exp.scan.get_num_images() == 1:
                if single_as_still:
                    exps_are_stills.append(True)
                elif exp.scan.get_oscillation()[1] == 0.0:
                    exps_are_stills.append(True)
                else:
                    exps_are_stills.append(False)
            else:
                if exp.scan.get_oscillation()[1] <= 0.0:
                    raise Sorry("Cannot refine a zero-width scan")
                exps_are_stills.append(False)

        # check experiment types are consistent
        if not all(exps_are_stills[0] == e for e in exps_are_stills):
            raise Sorry("Cannot refine a mixture of stills and scans")
        do_stills = exps_are_stills[0]

        # If experiments are stills, ensure scan-varying refinement won't be attempted
        if do_stills:
            params.refinement.parameterisation.scan_varying = False

        # calculate reflection block_width if required for scan-varying refinement
        if params.refinement.parameterisation.scan_varying:
            from dials.algorithms.refinement.reflection_manager import BlockCalculator

            block_calculator = BlockCalculator(experiments, reflections)
            if params.refinement.parameterisation.compose_model_per == "block":
                reflections = block_calculator.per_width(
                    params.refinement.parameterisation.block_width, deg=True
                )
            elif params.refinement.parameterisation.compose_model_per == "image":
                reflections = block_calculator.per_image()

        logger.debug("\nBuilding reflection manager")
        logger.debug("Input reflection list size = %d observations", len(reflections))

        # create reflection manager
        from dials.algorithms.refinement.reflection_manager import (
            ReflectionManagerFactory,
        )

        refman = ReflectionManagerFactory.from_parameters_reflections_experiments(
            params.refinement.reflections,
            reflections,
            experiments,
            do_stills,
            verbosity,
        )

        logger.debug(
            "Number of observations that pass initial inclusion criteria = %d",
            refman.get_accepted_refs_size(),
        )
        sample_size = refman.get_sample_size()
        if sample_size > 0:
            logger.debug("Working set size = %d observations", sample_size)
        logger.debug("Reflection manager built\n")

        # configure use of sparse data types
        params = cls.config_sparse(params, experiments)
        do_sparse = params.refinement.parameterisation.sparse

        # create managed reflection predictor
        from dials.algorithms.refinement.prediction.managed_predictors import (
            ExperimentsPredictorFactory,
        )

        ref_predictor = ExperimentsPredictorFactory.from_experiments(
            experiments,
            force_stills=do_stills,
            spherical_relp=params.refinement.parameterisation.spherical_relp_model,
        )

        # Predict for the managed observations, set columns for residuals and set
        # the used_in_refinement flag to the predictions
        obs = refman.get_obs()
        ref_predictor(obs)
        x_obs, y_obs, phi_obs = obs["xyzobs.mm.value"].parts()
        x_calc, y_calc, phi_calc = obs["xyzcal.mm"].parts()
        obs["x_resid"] = x_calc - x_obs
        obs["y_resid"] = y_calc - y_obs
        obs["phi_resid"] = phi_calc - phi_obs

        # determine whether to do basic centroid analysis to automatically
        # determine outlier rejection block
        if params.refinement.reflections.outlier.block_width is libtbx.Auto:
            ca = refman.get_centroid_analyser()
            analysis = ca(calc_average_residuals=False, calc_periodograms=False)
        else:
            analysis = None

        # Now predictions and centroid analysis are available, so we can finalise
        # the reflection manager
        refman.finalise(analysis)

        # Create model parameterisations
        logger.debug("Building prediction equation parameterisation")
        pred_param, param_reporter = cls.config_parameterisation(
            params.refinement.parameterisation, experiments, refman, do_stills
        )
        logger.debug("Prediction equation parameterisation built")
        logger.debug("Parameter order : name mapping")
        for i, e in enumerate(pred_param.get_param_names()):
            logger.debug("Parameter %03d : %s", i + 1, e)

        # Build a restraints parameterisation (if requested).
        # Only unit cell restraints are supported at the moment.
        restraints_parameterisation = cls.config_restraints(
            params.refinement.parameterisation, pred_param
        )

        # Build a constraints manager, if requested
        from dials.algorithms.refinement.constraints import ConstraintManagerFactory

        cmf = ConstraintManagerFactory(params, pred_param, verbosity)
        constraints_manager = cmf()

        # Create target function
        logger.debug("Building target function")
        target = cls.config_target(
            params.refinement.target,
            experiments,
            refman,
            ref_predictor,
            pred_param,
            restraints_parameterisation,
            do_stills,
            do_sparse,
        )
        logger.debug("Target function built")

        # create refinery
        logger.debug("Building refinement engine")
        refinery = cls.config_refinery(
            params, target, pred_param, constraints_manager, verbosity
        )
        logger.debug("Refinement engine built")

        # build refiner interface and return
        if params.refinement.parameterisation.scan_varying is True:
            refiner = ScanVaryingRefiner
        else:
            refiner = Refiner
        return refiner(
            reflections,
            experiments,
            pred_param,
            param_reporter,
            refman,
            target,
            refinery,
            verbosity=verbosity,
        )

    @staticmethod
    def config_sparse(params, experiments):
        """Configure whether to use sparse datatypes"""
        # Automatic selection for sparse parameter
        if params.refinement.parameterisation.sparse == libtbx.Auto:
            if len(experiments) > 1:
                params.refinement.parameterisation.sparse = True
            else:
                params.refinement.parameterisation.sparse = False
            if params.refinement.refinery.engine == "SparseLevMar":
                params.refinement.parameterisation.sparse = True
            if params.refinement.mp.nproc > 1:
                if params.refinement.refinery.engine != "SparseLevMar":
                    # sparse vectors cannot be pickled, so can't use easy_mp here
                    params.refinement.parameterisation.sparse = False
                else:
                    pass  # but SparseLevMar requires sparse jacobian; does not implement mp
        # Check incompatible selection
        elif (
            params.refinement.parameterisation.sparse and params.refinement.mp.nproc > 1
        ):
            logger.warning(
                "Could not set sparse=True and nproc={0}".format(
                    params.refinement.mp.nproc
                )
            )
            logger.warning("Resetting sparse=False")
            params.refinement.parameterisation.sparse = False
        return params

    @staticmethod
    def config_parameterisation(params, experiments, refman, do_stills=False):
        from dials.algorithms.refinement.parameterisation import (
            build_prediction_parameterisation,
        )

        pred_param = build_prediction_parameterisation(
            params, experiments, refman, do_stills
        )

        # Parameter reporting
        from dials.algorithms.refinement.parameterisation.parameter_report import (
            ParameterReporter,
        )

        param_reporter = ParameterReporter(
            pred_param.get_detector_parameterisations(),
            pred_param.get_beam_parameterisations(),
            pred_param.get_crystal_orientation_parameterisations(),
            pred_param.get_crystal_unit_cell_parameterisations(),
            pred_param.get_goniometer_parameterisations(),
        )

        return pred_param, param_reporter

    @staticmethod
    def config_restraints(params, pred_param):
        """Given a set of user parameters plus a model parameterisation, create
        restraints plus a parameterisation of these restraints

        Params:
            params: The input PHIL parameters
            pred_param: A PredictionParameters object

        Returns:
            A restraints parameterisation or None
        """

        if not any(
            [
                params.crystal.unit_cell.restraints.tie_to_target,
                params.crystal.unit_cell.restraints.tie_to_group,
            ]
        ):
            return None

        det_params = pred_param.get_detector_parameterisations()
        beam_params = pred_param.get_beam_parameterisations()
        xl_ori_params = pred_param.get_crystal_orientation_parameterisations()
        xl_uc_params = pred_param.get_crystal_unit_cell_parameterisations()
        gon_params = pred_param.get_goniometer_parameterisations()

        from dials.algorithms.refinement.restraints import RestraintsParameterisation

        rp = RestraintsParameterisation(
            detector_parameterisations=det_params,
            beam_parameterisations=beam_params,
            xl_orientation_parameterisations=xl_ori_params,
            xl_unit_cell_parameterisations=xl_uc_params,
            goniometer_parameterisations=gon_params,
        )

        # Shorten params path
        # FIXME Only unit cell restraints currently supported
        # beam_r = params.beam.restraints
        cell_r = params.crystal.unit_cell.restraints
        # orientation_r = params.crystal.orientation.restraints
        # detector_r = params.detector.restraints

        for tie in cell_r.tie_to_target:
            if len(tie.values) != 6:
                raise Sorry(
                    "6 cell parameters must be provided as the tie_to_target.values."
                )
            if len(tie.sigmas) != 6:
                raise Sorry(
                    "6 sigmas must be provided as the tie_to_target.sigmas. "
                    "Note that individual sigmas of 0.0 will remove "
                    "the restraint for the corresponding cell parameter."
                )
            if tie.id is None:
                # get one experiment id for each parameterisation to apply to all
                tie.id = [e.get_experiment_ids()[0] for e in xl_uc_params]
            for exp_id in tie.id:
                rp.add_restraints_to_target_xl_unit_cell(exp_id, tie.values, tie.sigmas)

        for tie in cell_r.tie_to_group:
            if len(tie.sigmas) != 6:
                raise Sorry(
                    "6 sigmas must be provided as the tie_to_group.sigmas. "
                    "Note that individual sigmas of 0.0 will remove "
                    "the restraint for the corresponding cell parameter."
                )
            if tie.id is None:
                rp.add_restraints_to_group_xl_unit_cell(tie.target, "all", tie.sigmas)
            else:
                rp.add_restraints_to_group_xl_unit_cell(tie.target, tie.id, tie.sigmas)

        return rp

    @staticmethod
    def config_refinery(params, target, pred_param, constraints_manager, verbosity):
        """Given a set of parameters, a target class, a prediction
        parameterisation class and a constraints_manager (which could be None),
        build a refinery

        Params:
            params The input parameters

        Returns:
            The refinery instance
        """

        # Shorten parameter path
        options = params.refinement.refinery

        if options.engine == "SimpleLBFGS":
            from dials.algorithms.refinement.engine import SimpleLBFGS as refinery
        elif options.engine == "LBFGScurvs":
            from dials.algorithms.refinement.engine import LBFGScurvs as refinery
        elif options.engine == "GaussNewton":
            from dials.algorithms.refinement.engine import (
                GaussNewtonIterations as refinery,
            )
        elif options.engine == "LevMar":
            from dials.algorithms.refinement.engine import (
                LevenbergMarquardtIterations as refinery,
            )
        elif options.engine == "SparseLevMar":
            from dials.algorithms.refinement.sparse_engine import (
                SparseLevenbergMarquardtIterations as refinery,
            )
        else:
            raise RuntimeError(
                "Refinement engine " + options.engine + " not recognised"
            )

        logger.debug("Selected refinement engine type: %s", options.engine)

        engine = refinery(
            target=target,
            prediction_parameterisation=pred_param,
            constraints_manager=constraints_manager,
            log=options.log,
            verbosity=verbosity,
            tracking=options.journal,
            max_iterations=options.max_iterations,
        )

        if params.refinement.mp.nproc > 1:
            nproc = params.refinement.mp.nproc
            try:
                engine.set_nproc(nproc)
            except NotImplementedError:
                logger.warning(
                    "Could not set nproc={0} for refinement engine of type {1}".format(
                        nproc, options.engine
                    )
                )

        return engine

    # Overload to allow subclasses of RefinerFactory to use a different
    # TargetFactory
    @staticmethod
    def config_target(
        params,
        experiments,
        reflection_manager,
        predictor,
        pred_param,
        restraints_param,
        do_stills,
        do_sparse,
    ):

        from dials.algorithms.refinement.target import TargetFactory

        target = TargetFactory.from_parameters_and_experiments(
            params,
            experiments,
            reflection_manager,
            predictor,
            pred_param,
            restraints_param,
            do_stills,
            do_sparse,
        )
        return target


class Refiner(object):
    """Public interface for performing DIALS refinement.

    Public methods:
      run
      rmsds
      get_experiments
      get_matches
      get_param_reporter
      parameter_correlation_plot
      selection_used_for_refinement
      predict_for_reflection_table
      predict_for_indexed

    Notes:
      * The return value of run is a recorded history of the refinement
      * The experiments accessor provides a copy of the experiments used by
        refinement
      * The model accessors provide copies of those models that might be modified
        by refinement (beam, crystal and detector) TO BE DEPRECATED
      * get_matches exposes the function of the same name from the privately
        stored reflection manager
      * The return value of selection_used_for_refinement is a flex.bool

    """

    def __init__(
        self,
        reflections,
        experiments,
        pred_param,
        param_reporter,
        refman,
        target,
        refinery,
        verbosity=0,
    ):
        """
        Mandatory arguments:
          reflections - Input ReflectionList data
          experiments - a dxtbx ExperimentList object
          pred_param - An object derived from the PredictionParameterisation class
          param_reporter -A ParameterReporter object
          refman - A ReflectionManager object
          target - An object derived from the Target class
          refinery - An object derived from the Refinery class

        Optional arguments:
          verbosity - An integer verbosity level

        """

        # the experimental models
        self._experiments = experiments

        # refinement module main objects
        self._pred_param = pred_param
        self._refman = refman
        self._target = target
        self._refinery = refinery

        # parameter reporter
        self._param_report = param_reporter

        self._verbosity = verbosity

        return

    def get_experiments(self):
        """Return a copy of the current refiner experiments"""
        return _copy_experiments_for_refining(self._experiments)

    def rmsds(self):
        """Return rmsds of the current model"""

        # ensure predictions for the matches are up to date
        self._refinery.prepare_for_step()

        return self._target.rmsds()

    def rmsds_for_reflection_table(self, reflections):
        """Calculate unweighted RMSDs for the specified reflections"""

        # ensure predictions for these reflections are up to date
        preds = self.predict_for_reflection_table(reflections)

        return self._target.rmsds_for_reflection_table(preds)

    def get_matches(self):
        """Delegated to the reflection manager"""

        # FIXME Consider: Does this information really need to be exposed by the
        # public API (indexing code seems to use it, but is it necessary?)
        return self._refman.get_matches()

    def get_free_reflections(self):
        """Delegated to the reflection manager"""

        return self._refman.get_free_reflections()

    def get_param_reporter(self):
        """Get the ParameterReport object linked to this Refiner"""

        return self._param_report

    def get_parameter_correlation_matrix(self, step, col_select=None):
        """Return the correlation matrix between columns of the Jacobian at
        the specified refinement step. The parameter col_select can be used
        to select subsets of the full number of columns. The column labels
        are also returned as a list of strings"""

        corrmats = self._refinery.get_correlation_matrix_for_step(step)
        if corrmats is None:
            return None, None

        all_labels = self._pred_param.get_param_names()
        from dials.algorithms.refinement.refinement_helpers import string_sel

        if col_select is None:
            col_select = range(len(all_labels))
        sel = string_sel(col_select, all_labels)
        labels = [e for e, s in zip(all_labels, sel) if s]
        num_cols = num_rows = len(labels)
        if num_cols == 0:
            return None, None

        for k, corrmat in corrmats.items():

            assert corrmat.is_square_matrix()

            from scitbx.array_family import flex

            idx = flex.bool(sel).iselection()
            sub_corrmat = flex.double(flex.grid(num_cols, num_cols))

            for (i, x) in enumerate(idx):
                for (j, y) in enumerate(idx):
                    sub_corrmat[i, j] = corrmat[x, y]

            corrmats[k] = sub_corrmat

        return (corrmats, labels)

    def print_step_table(self):
        """print useful output about refinement steps in the form of a simple table"""

        from libtbx.table_utils import simple_table
        from math import pi

        rad2deg = 180 / pi

        logger.info("\nRefinement steps:")

        rmsd_multipliers = []
        header = ["Step", "Nref"]
        for (name, units) in zip(self._target.rmsd_names, self._target.rmsd_units):
            if units == "mm":
                header.append(name + "\n(mm)")
                rmsd_multipliers.append(1.0)
            elif units == "rad":  # convert radians to degrees for reporting
                header.append(name + "\n(deg)")
                rmsd_multipliers.append(rad2deg)
            else:  # leave unknown units alone
                header.append(name + "\n(" + units + ")")

        rows = []
        for i in range(self._refinery.history.get_nrows()):
            rmsds = [
                r * m
                for (r, m) in zip(self._refinery.history["rmsd"][i], rmsd_multipliers)
            ]
            rows.append(
                [str(i), str(self._refinery.history["num_reflections"][i])]
                + ["%.5g" % r for r in rmsds]
            )

        st = simple_table(rows, header)
        logger.info(st.format())
        logger.info(self._refinery.history.reason_for_termination)

        return

    def print_out_of_sample_rmsd_table(self):
        """print out-of-sample RSMDs per step, if these were tracked"""

        from libtbx.table_utils import simple_table
        from math import pi

        rad2deg = 180 / pi

        # check if it makes sense to proceed
        if not "out_of_sample_rmsd" in self._refinery.history:
            return
        nref = len(self.get_free_reflections())
        if nref < 10:
            return  # don't do anything if very few refs

        logger.info("\nRMSDs for out-of-sample (free) reflections:")

        rmsd_multipliers = []
        header = ["Step", "Nref"]
        for (name, units) in zip(self._target.rmsd_names, self._target.rmsd_units):
            if units == "mm":
                header.append(name + "\n(mm)")
                rmsd_multipliers.append(1.0)
            elif units == "rad":  # convert radians to degrees for reporting
                header.append(name + "\n(deg)")
                rmsd_multipliers.append(rad2deg)
            else:  # leave unknown units alone
                header.append(name + "\n(" + units + ")")

        rows = []
        for i in range(self._refinery.history.get_nrows()):
            rmsds = [
                r * m
                for r, m in zip(
                    self._refinery.history["out_of_sample_rmsd"][i], rmsd_multipliers
                )
            ]
            rows.append([str(i), str(nref)] + ["%.5g" % e for e in rmsds])

        st = simple_table(rows, header)
        logger.info(st.format())

        return

    def print_exp_rmsd_table(self):
        """print useful output about refinement steps in the form of a simple table"""

        from libtbx.table_utils import simple_table
        from math import pi

        rad2deg = 180 / pi

        logger.info("\nRMSDs by experiment:")

        header = ["Exp\nid", "Nref"]
        for (name, units) in zip(self._target.rmsd_names, self._target.rmsd_units):
            if name == "RMSD_X" or name == "RMSD_Y" and units == "mm":
                header.append(name + "\n(px)")
            elif name == "RMSD_Phi" and units == "rad":
                # will convert radians to images for reporting of scans
                header.append("RMSD_Z" + "\n(images)")
            elif units == "rad":
                # will convert other angles in radians to degrees (e.g. for
                # RMSD_DeltaPsi and RMSD_2theta)
                header.append(name + "\n(deg)")
            else:  # skip other/unknown RMSDs
                pass

        rows = []
        for iexp, exp in enumerate(self._experiments):
            detector = exp.detector
            px_sizes = [p.get_pixel_size() for p in detector]
            it = iter(px_sizes)
            px_size = next(it)
            if not all(tst == px_size for tst in it):
                logger.info(
                    "The detector in experiment %d does not have the same pixel "
                    + "sizes on each panel. Skipping...",
                    iexp,
                )
                continue
            px_per_mm = [1.0 / e for e in px_size]

            scan = exp.scan
            try:
                temp = scan.get_oscillation(deg=False)
                images_per_rad = 1.0 / abs(scan.get_oscillation(deg=False)[1])
            except (AttributeError, ZeroDivisionError):
                images_per_rad = None

            raw_rmsds = self._target.rmsds_for_experiment(iexp)
            if raw_rmsds is None:
                continue  # skip experiments where rmsd cannot be calculated
            num = self._target.get_num_matches_for_experiment(iexp)
            rmsds = []
            for (name, units, rmsd) in zip(
                self._target.rmsd_names, self._target.rmsd_units, raw_rmsds
            ):
                if name == "RMSD_X" and units == "mm":
                    rmsds.append(rmsd * px_per_mm[0])
                elif name == "RMSD_Y" and units == "mm":
                    rmsds.append(rmsd * px_per_mm[1])
                elif name == "RMSD_Phi" and units == "rad":
                    rmsds.append(rmsd * images_per_rad)
                elif units == "rad":
                    rmsds.append(rmsd * rad2deg)
            rows.append([str(iexp), str(num)] + ["%.5g" % r for r in rmsds])

        if len(rows) > 0:
            truncated = False
            max_rows = 100
            if self._verbosity < 3 and len(rows) > max_rows:
                rows = rows[0:max_rows]
                truncated = True
            st = simple_table(rows, header)
            logger.info(st.format())
            if truncated:
                logger.info(
                    "Table truncated to show the first %d experiments only", max_rows
                )
                logger.info("Re-run with verbosity >= 3 to show all experiments")

        return

    def print_panel_rmsd_table(self):
        """print useful output about refinement steps in the form of a simple table"""

        from libtbx.table_utils import simple_table
        from math import pi

        rad2deg = 180 / pi

        if len(self._experiments.scans()) > 1:
            logger.warning(
                "Multiple scans present. Only the first scan will be used "
                "to determine the image width for reporting RMSDs"
            )
        scan = self._experiments.scans()[0]
        try:
            temp = scan.get_oscillation(deg=False)
            images_per_rad = 1.0 / abs(scan.get_oscillation(deg=False)[1])
        except AttributeError:
            images_per_rad = None

        for idetector, detector in enumerate(self._experiments.detectors()):
            if len(detector) == 1:
                continue
            logger.info("\nDetector {0} RMSDs by panel:".format(idetector + 1))

            header = ["Panel\nid", "Nref"]
            for (name, units) in zip(self._target.rmsd_names, self._target.rmsd_units):
                if name == "RMSD_X" or name == "RMSD_Y" and units == "mm":
                    header.append(name + "\n(px)")
                elif (
                    name == "RMSD_Phi" and units == "rad"
                ):  # convert radians to images for reporting of scans
                    header.append("RMSD_Z" + "\n(images)")
                elif (
                    name == "RMSD_DeltaPsi" and units == "rad"
                ):  # convert radians to degrees for reporting of stills
                    header.append(name + "\n(deg)")
                else:  # skip RMSDs that cannot be expressed in image/scan space
                    pass

            rows = []
            for ipanel, panel in enumerate(detector):

                px_size = panel.get_pixel_size()
                px_per_mm = [1.0 / e for e in px_size]
                num = self._target.get_num_matches_for_panel(ipanel)
                if num <= 0:
                    continue
                raw_rmsds = self._target.rmsds_for_panel(ipanel)
                if raw_rmsds is None:
                    continue  # skip panels where rmsd cannot be calculated
                rmsds = []
                for (name, units, rmsd) in zip(
                    self._target.rmsd_names, self._target.rmsd_units, raw_rmsds
                ):
                    if name == "RMSD_X" and units == "mm":
                        rmsds.append(rmsd * px_per_mm[0])
                    elif name == "RMSD_Y" and units == "mm":
                        rmsds.append(rmsd * px_per_mm[1])
                    elif name == "RMSD_Phi" and units == "rad":
                        rmsds.append(rmsd * images_per_rad)
                    elif name == "RMSD_DeltaPsi" and units == "rad":
                        rmsds.append(rmsd * rad2deg)
                rows.append([str(ipanel), str(num)] + ["%.5g" % r for r in rmsds])

            if len(rows) > 0:
                st = simple_table(rows, header)
                logger.info(st.format())

        return

    def run(self):
        """Run refinement"""

        ####################################
        # Do refinement and return history #
        ####################################

        if self._verbosity > 1:
            logger.debug("\nExperimental models before refinement:")
            for i, beam in enumerate(self._experiments.beams()):
                logger.debug(ordinal_number(i) + " " + str(beam))
            for i, detector in enumerate(self._experiments.detectors()):
                logger.debug(ordinal_number(i) + " " + str(detector))
            for i, goniometer in enumerate(self._experiments.goniometers()):
                if goniometer is None:
                    continue
                logger.debug(ordinal_number(i) + " " + str(goniometer))
            for i, scan in enumerate(self._experiments.scans()):
                if scan is None:
                    continue
                logger.debug(ordinal_number(i) + " " + str(scan))
            for i, crystal in enumerate(self._experiments.crystals()):
                logger.debug(ordinal_number(i) + " " + str(crystal))

        self._refinery.run()

        # These involve calculation, so skip them when verbosity is zero
        if self._verbosity > 0:
            self.print_step_table()
            self.print_out_of_sample_rmsd_table()
            self.print_exp_rmsd_table()

        det_npanels = [len(d) for d in self._experiments.detectors()]
        if any(n > 1 for n in det_npanels):
            self.print_panel_rmsd_table()

        # Perform post-run tasks to write the refined states back to the models
        self._update_models()

        if self._verbosity > 1:
            logger.debug("\nExperimental models after refinement:")
            for i, beam in enumerate(self._experiments.beams()):
                logger.debug(ordinal_number(i) + " " + str(beam))
            for i, detector in enumerate(self._experiments.detectors()):
                logger.debug(ordinal_number(i) + " " + str(detector))
            for i, goniometer in enumerate(self._experiments.goniometers()):
                if goniometer is None:
                    continue
                logger.debug(ordinal_number(i) + " " + str(goniometer))
            for i, scan in enumerate(self._experiments.scans()):
                if scan is None:
                    continue
                logger.debug(ordinal_number(i) + " " + str(scan))
            for i, crystal in enumerate(self._experiments.crystals()):
                logger.debug(ordinal_number(i) + " " + str(crystal))

            # Report on the refined parameters
            logger.debug(str(self._param_report))

        # Return the refinement history
        return self._refinery.history

    def _update_models(self):
        """Perform any extra tasks required to update the models after refinement.
        Does nothing here, but used by subclasses"""
        pass

    def selection_used_for_refinement(self):
        """Return a selection as a flex.bool in terms of the input reflection
        data of those reflections that were used in the final step of
        refinement."""

        from scitbx.array_family import flex

        matches = self._refman.get_matches()
        selection = flex.bool(len(self._refman.get_indexed()), False)

        try:  # new reflection table format for matches
            isel = matches["iobs"]
            selection.set_selected(isel, True)
        except TypeError:  # old ObsPredMatch format for matches
            for m in matches:
                selection[m.iobs] = True

        return selection

    def predict_for_indexed(self):
        """perform prediction for all the indexed reflections passed into
        refinement and additionally set the used_in_refinement flag. Do not
        compose the derivatives of states of the model as this is expensive and
        they are not needed outside of a refinement run"""

        reflections = self.predict_for_reflection_table(
            self._refman.get_indexed(), skip_derivatives=True
        )
        reflections.sort("iobs")
        mask = self.selection_used_for_refinement()
        reflections.set_flags(mask, reflections.flags.used_in_refinement)
        return reflections

    def predict_for_reflection_table(self, reflections, skip_derivatives=False):
        """perform prediction for all reflections in the supplied table"""

        # delegate to the target object, which has access to the predictor
        return self._target.predict_for_reflection_table(reflections, skip_derivatives)


class ScanVaryingRefiner(Refiner):
    """Includes functionality to update the models with their states at
    scan-points after scan-varying refinement"""

    def _update_models(self):
        for iexp, exp in enumerate(self._experiments):
            ar_range = exp.scan.get_array_range()
            obs_image_numbers = range(ar_range[0], ar_range[1] + 1)

            # write scan-varying s0 vectors back to beam models
            s0_list = self._pred_param.get_varying_s0(obs_image_numbers, iexp)
            if s0_list is not None:
                exp.beam.set_s0_at_scan_points(s0_list)

            # write scan-varying setting rotation matrices back to goniometer models
            S_list = self._pred_param.get_varying_setting_rotation(
                obs_image_numbers, iexp
            )
            if S_list is not None:
                exp.goniometer.set_setting_rotation_at_scan_points(S_list)

            # write scan-varying crystal setting matrices back to crystal models
            A_list = self._pred_param.get_varying_UB(obs_image_numbers, iexp)
            if A_list is not None:
                exp.crystal.set_A_at_scan_points(A_list)

            # Return early if not calculating scan-varying errors
            if not self._pred_param.set_scan_varying_errors:
                return

            # get state covariance matrices the whole range of images. We select
            # the first element of this at each image because crystal scan-varying
            # parameterisations are not multi-state
            state_cov_list = [
                self._pred_param.calculate_model_state_uncertainties(
                    obs_image_number=t, experiment_id=iexp
                )
                for t in range(ar_range[0], ar_range[1] + 1)
            ]
            if "U_cov" in state_cov_list[0]:
                u_cov_list = [e["U_cov"] for e in state_cov_list]
            else:
                u_cov_list = None

            if "B_cov" in state_cov_list[0]:
                b_cov_list = [e["B_cov"] for e in state_cov_list]
            else:
                b_cov_list = None

            # return these to the model parameterisations to be set in the models
            self._pred_param.set_model_state_uncertainties(u_cov_list, b_cov_list, iexp)
