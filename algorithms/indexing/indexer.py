#!/usr/bin/env python
# -*- mode: python; coding: utf-8; indent-tabs-mode: nil; python-indent: 2 -*-
#
# dials.algorithms.indexing.indexer.py
#
#  Copyright (C) 2014 Diamond Light Source
#
#  Author: Richard Gildea
#
#  This code is distributed under the BSD license, a copy of which is
#  included in the root directory of this package.

from __future__ import absolute_import, division
from __future__ import print_function
import math
import logging

logger = logging.getLogger(__name__)

from dials.util import log

debug_handle = log.debug_handle(logger)

import libtbx
from dials.util import Sorry
import iotbx.phil
from scitbx import matrix

from dials.array_family import flex
from dials.algorithms.indexing import assign_indices
from dials.algorithms.indexing.compare_orientation_matrices import (
    difference_rotation_matrix_axis_angle,
)
from dials.algorithms.indexing.symmetry import SymmetryHandler

from cctbx import sgtbx

from dxtbx.model import ExperimentList

from dials.algorithms.indexing.max_cell import find_max_cell

max_cell_phil_str = """\
max_cell_estimation
  .expert_level = 1
{
  filter_ice = True
    .type = bool
    .help = "Filter out reflections at typical ice ring resolutions"
            "before max_cell estimation."
  filter_overlaps = True
    .type = bool
    .help = "Filter out reflections with overlapping bounding boxes before"
            "max_cell estimation."
  overlaps_border = 0
    .type = int(value_min=0)
    .help = "Optionally add a border around the bounding boxes before finding"
            "overlaps."
  multiplier = 1.3
    .type = float(value_min=0)
    .help = "Multiply the estimated maximum basis vector length by this value."
    .expert_level = 2
  step_size = 45
    .type = float(value_min=0)
    .help = "Step size, in degrees, of the blocks used to peform the max_cell "
            "estimation."
    .expert_level = 2
  nearest_neighbor_percentile = None
    .type = float(value_min=0, value_max=1)
    .help = "Percentile of NN histogram to use for max cell determination."
    .expert_level = 2
  histogram_binning = linear *log
    .type = choice
    .help = "Choose between linear or logarithmic bins for nearest neighbour"
            "histogram analysis."
  nn_per_bin = 5
    .type = int(value_min=1)
    .help = "Target number of nearest neighbours per histogram bin."
  max_height_fraction = 0.25
    .type = float(value_min=0, value_max=1)
    .expert_level=2
}
"""

index_only_phil_str = (
    """\
indexing {
  nproc = 1
    .type = int(value_min=1)
    .help = "The number of processes to use."
  mm_search_scope = 4.0
    .help = "Global radius of origin offset search."
    .type = float(value_min=0)
    .expert_level = 1
  wide_search_binning = 2
    .help = "Modify the coarseness of the wide grid search for the beam centre."
    .type = float(value_min=0)
    .expert_level = 1
  min_cell_volume = 25
    .type = float(value_min=0)
    .help = "Minimum unit cell volume (in Angstrom^3)."
    .expert_level = 1
  min_cell = 3
    .type = float(value_min=0)
    .help = "Minimum length of candidate unit cell basis vectors (in Angstrom)."
    .expert_level = 1
  max_cell = Auto
    .type = float(value_min=0)
    .help = "Maximum length of candidate unit cell basis vectors (in Angstrom)."
    .expert_level = 1
  %s
  fft3d {
    peak_search = *flood_fill clean
      .type = choice
      .expert_level = 2
    peak_volume_cutoff = 0.15
      .type = float
      .expert_level = 2
    reciprocal_space_grid {
      n_points = 256
        .type = int(value_min=0)
        .expert_level = 1
      d_min = Auto
        .type = float(value_min=0)
        .help = "The high resolution limit in Angstrom for spots to include in "
                "the initial indexing."
    }
  }
  sigma_phi_deg = None
    .type = float(value_min=0)
    .help = "Override the phi sigmas for refinement. Mainly intended for single-shot"
            "rotation images where the phi sigma is almost certainly incorrect."
    .expert_level = 2
  b_iso = Auto
    .type = float(value_min=0)
    .expert_level = 2
  rmsd_cutoff = 15
    .type = float(value_min=0)
    .expert_level = 1
  scan_range = None
    .help = "The range of images to use in indexing. Number of arguments"
            "must be a factor of two. Specifying \"0 0\" will use all images"
            "by default. The given range follows C conventions"
            "(e.g. j0 <= j < j1)."
    .type = ints(size=2)
    .multiple = True
  known_symmetry {
    space_group = None
      .type = space_group
      .help = "Target space group for indexing."
    unit_cell = None
      .type = unit_cell
      .help = "Target unit cell for indexing."
    relative_length_tolerance = 0.1
      .type = float
      .help = "Relative tolerance for unit cell lengths in unit cell comparision."
      .expert_level = 1
    absolute_angle_tolerance = 5
      .type = float
      .help = "Angular tolerance (in degrees) in unit cell comparison."
      .expert_level = 1
    max_delta = 5
      .type = float(value_min=0)
      .help = "Maximum allowed Le Page delta used in searching for basis vector"
              "combinations that are consistent with the given symmetry."
      .expert_level = 1
  }
  basis_vector_combinations
    .expert_level = 1
  {
    max_combinations = None
      .type = int(value_min=1)
      .help = "Maximum number of basis vector combinations to test for agreement"
              "with input symmetry."
    max_refine = Auto
      .type = int(value_min=1)
      .help = "Maximum number of putative crystal models to test. Default"
              "for rotation sweeps: 50, for still images: 5"
      .expert_level = 1
    sys_absent_threshold = 0.9
      .type = float(value_min=0.0, value_max=1.0)
    solution_scorer = filter *weighted
      .type = choice
      .expert_level = 1
    filter
      .expert_level = 1
    {
      check_doubled_cell = True
        .type = bool
      likelihood_cutoff = 0.8
        .type = float(value_min=0, value_max=1)
      volume_cutoff = 1.25
        .type = float(value_min=1)
      n_indexed_cutoff = 0.9
        .type = float(value_min=0, value_max=1)
    }
    weighted
      .expert_level = 1
    {
      power = 1
        .type = int(value_min=1)
      volume_weight = 1
        .type = float(value_min=0)
      n_indexed_weight = 1
        .type = float(value_min=0)
      rmsd_weight = 1
        .type = float(value_min=0)
    }
  }
  index_assignment {
    method = *simple local
      .type = choice
      .help = "Choose between simple 'global' index assignment and xds-style "
              "'local' index assignment."
      .expert_level = 1
    simple {
      hkl_tolerance = 0.3
        .type = float(value_min=0, value_max=0.5)
        .help = "Maximum allowable deviation from integer-ness for assigning "
                "a miller index to a reciprocal lattice vector."
    }
    local
      .expert_level = 1
    {
      epsilon = 0.05
        .type = float
        .help = "This corresponds to the xds parameter INDEX_ERROR="
      delta = 8
        .type = int
        .help = "This corresponds to the xds parameter INDEX_MAGNITUDE="
      l_min = 0.8
        .type = float
        .help = "This corresponds to the xds parameter INDEX_QUALITY="
      nearest_neighbours = 20
        .type = int(value_min=1)
    }
  }
  check_misindexing {
    grid_search_scope = 0
      .type = int
      .help = "Search scope for testing misindexing on h, k, l."
  }
  optimise_initial_basis_vectors = False
    .type = bool
    .expert_level = 2
  debug = False
    .type = bool
    .expert_level = 1
  debug_plots = False
    .type = bool
    .help = "Requires matplotlib"
    .expert_level = 1
  combine_scans = False
    .type = bool
    .expert_level = 1
  refinement_protocol {
    mode = *refine_shells repredict_only
      .type = choice
      .expert_level = 1
      .help = "refine_shells: refine in increasing resolution cutoffs after indexing."
              "repredict_only: do not refine after indexing, just update spot"
              "predictions."
    n_macro_cycles = 5
      .type = int(value_min=1)
      .help = "Maximum number of macro cycles of refinement, reindexing all"
              "reflections using updated geometry at the beginning of each"
              "cycle. Does not apply to stills.indexer=stills."
    d_min_step = Auto
      .type = float(value_min=0.0)
      .help = "Reduction per step in d_min for reflections to include in refinement."
    d_min_start = None
      .type = float(value_min=0.0)
    d_min_final = None
      .type = float(value_min=0.0)
      .help = "Do not ever include reflections below this value in refinement."
    verbosity = 1
      .type = int(value_min=0)
    disable_unit_cell_volume_sanity_check = False
      .type = bool
      .help = "Disable sanity check on unrealistic increases in unit cell volume"
              "during refinement."
      .expert_level = 1
  }
  method = *fft3d fft1d real_space_grid_search
    .type = choice
  multiple_lattice_search
    .expert_level = 1
  {
    cluster_analysis_search = False
      .type = bool
      .help = "Perform cluster analysis search for multiple lattices."
    recycle_unindexed_reflections_cutoff = 0.1
      .type = float(value_min=0, value_max=1)
      .help = "Attempt another cycle of indexing on the unindexed reflections "
              "if more than the fraction of input reflections are unindexed."
    minimum_angular_separation = 5
      .type = float(value_min=0)
      .help = "The minimum angular separation (in degrees) between two lattices."
    max_lattices = 1
      .type = int
    cluster_analysis {
      method = *dbscan hcluster
        .type = choice
      hcluster {
        linkage {
          method = *ward
            .type = choice
          metric = *euclidean
            .type = choice
        }
        cutoff = 15
          .type = float(value_min=0)
        cutoff_criterion = *distance inconsistent
          .type = choice
      }
      dbscan {
        eps = 0.05
          .type = float(value_min=0.0)
        min_samples = 30
          .type = int(value_min=1)
      }
      min_cluster_size = 20
        .type = int(value_min=0)
      intersection_union_ratio_cutoff = 0.4
        .type = float(value_min=0.0, value_max=1.0)
    }
  }
  fft1d
    .expert_level = 1
  {
    characteristic_grid = None
      .help = Sampling frequency in radians. See Steller 1997. If None, \
              determine a grid sampling automatically using the input \
              reflections, using at most 0.029 radians.
      .type = float(value_min=0)
  }
  real_space_grid_search
    .expert_level = 1
  {
    characteristic_grid = 0.02
      .type = float(value_min=0)
  }
  stills {
    indexer = *Auto stills sweeps
      .type = choice
      .help = Use the stills or sweeps indexer.  Auto: choose based on the input \
              imagesets (stills or sweeps).
      .expert_level = 1
    ewald_proximity_resolution_cutoff = 2.0
      .type = float
      .help = For still images, this high-resolution cutoff is used to calculate
      .help = the acceptable volume of reciprocal space for spot prediction
    refine_all_candidates = True
      .type = bool
      .help = If False, no attempt is made to refine the model from initial basis \
              vector selection. The indexing solution with the best RMSD is chosen.
    candidate_outlier_rejection = True
      .type = bool
      .expert_level = 1
      .help = If True, while refining candiate basis solutions, also apply Sauter/ \
              Poon (2010) outlier rejection
    refine_candidates_with_known_symmetry = False
      .type = bool
      .expert_level = 2
      .help = If False, when choosing the best set of candidate basis solutions, \
              refine the candidates in the P1 setting. If True, after indexing \
              in P1, convert the candidates to the known symmetry and apply the \
              corresponding change of basis to the indexed reflections.
    rmsd_min_px = 2
      .type = float
      .help = Minimum acceptable RMSD for choosing candidate basis solutions \
              (in pixels)
    ewald_proximal_volume_max = 0.0025
      .type = float
      .help = Maximum acceptable ewald proximal volume when choosing candidate \
              basis solutions
    isoforms
      .help = Constrain the unit cell to specific values during refinement after initial indexing.
      .multiple=True
    {
      name=None
        .type=str
      cell=None
        .type=unit_cell
      lookup_symbol=None
        .type=str
        .help=The sgtbx lookup symbol of the reflections pointgroup
      rmsd_target_mm=None
        .type=float
        .help=Maximum acceptable DIALS positional rmsd, in mm
      beam_restraint=None
        .type=floats(size=2)
        .help=Known beam position in mm X,Y, rmsd_target_mm is reused here as a circle of confusion
        .help=to assure that no images are accepted where the lattice is misindexed by a unit shift.
    }
  }
}
"""
    % max_cell_phil_str
)

index_only_phil_scope = iotbx.phil.parse(index_only_phil_str, process_includes=True)

master_phil_scope = iotbx.phil.parse(
    """
%s
include scope dials.algorithms.refinement.refiner.phil_scope
"""
    % index_only_phil_str,
    process_includes=True,
)

# override default refinement parameters
master_phil_scope = master_phil_scope.fetch(
    source=iotbx.phil.parse(
        """\
refinement {
  reflections {
    reflections_per_degree=100
  }
}
"""
    )
)

master_params = master_phil_scope.fetch().extract()


def filter_reflections_by_scan_range(reflections, scan_range):
    reflections_in_scan_range = flex.bool(len(reflections), False)
    frame_number = reflections["xyzobs.px.value"].parts()[2]

    for scan_range in scan_range:
        if scan_range is None:
            continue
        range_start, range_end = scan_range
        reflections_in_scan_range.set_selected(
            (frame_number >= range_start) & (frame_number < range_end), True
        )
    return reflections.select(reflections_in_scan_range)


class vector_group(object):
    def __init__(self):
        self.vectors = []
        self.lengths = []
        self.volumes = []
        self._mean = None

    def append(self, vector, length, volume):
        self.vectors.append(vector)
        self.lengths.append(length)
        self.volumes.append(volume)
        self._mean = self.compute_mean()

    def mean(self):
        if self._mean is None:
            self._mean = self.compute_mean()
        return self._mean

    def compute_mean(self):
        sum_x = 0
        sum_y = 0
        sum_z = 0
        for v in self.vectors:
            sum_x += v.elems[0]
            sum_y += v.elems[1]
            sum_z += v.elems[2]
        return matrix.col((sum_x, sum_y, sum_z)) / len(self.vectors)


def is_approximate_integer_multiple(
    vec_a, vec_b, relative_tolerance=0.2, angular_tolerance=5.0
):
    length_a = vec_a.length()
    length_b = vec_b.length()
    # assert length_b >= length_a
    if length_a > length_b:
        vec_a, vec_b = vec_b, vec_a
        length_a, length_b = length_b, length_a
    angle = vec_a.angle(vec_b, deg=True)
    if angle < angular_tolerance or abs(180 - angle) < angular_tolerance:
        n = length_b / length_a
        if abs(round(n) - n) < relative_tolerance:
            return True
    return False


deg_to_radians = math.pi / 180


class indexer_base(object):
    def __init__(self, reflections, experiments, params=None):
        self.reflections = reflections
        self.experiments = experiments

        if params is None:
            params = master_params

        self.params = params.indexing
        self.all_params = params
        self.refined_experiments = None
        self.hkl_offset = None

        if self.params.index_assignment.method == "local":
            self._assign_indices = assign_indices.assign_indices_local(
                epsilon=self.params.index_assignment.local.epsilon,
                delta=self.params.index_assignment.local.delta,
                l_min=self.params.index_assignment.local.l_min,
                nearest_neighbours=self.params.index_assignment.local.nearest_neighbours,
            )
        else:
            self._assign_indices = assign_indices.assign_indices_global(
                tolerance=self.params.index_assignment.simple.hkl_tolerance
            )

        if self.all_params.refinement.reflections.outlier.algorithm in (
            "auto",
            libtbx.Auto,
        ):
            if self.experiments[0].goniometer is None:
                self.all_params.refinement.reflections.outlier.algorithm = "sauter_poon"
            else:
                # different default to dials.refine
                # tukey is faster and more appropriate at the indexing step
                self.all_params.refinement.reflections.outlier.algorithm = "tukey"

        for expt in self.experiments[1:]:
            if expt.detector.is_similar_to(self.experiments[0].detector):
                expt.detector = self.experiments[0].detector
            if expt.goniometer is not None and expt.goniometer.is_similar_to(
                self.experiments[0].goniometer
            ):
                expt.goniometer = self.experiments[0].goniometer
                # can only share a beam if we share a goniometer?
                if expt.beam.is_similar_to(self.experiments[0].beam):
                    expt.beam = self.experiments[0].beam
                if self.params.combine_scans and expt.scan == self.experiments[0].scan:
                    expt.scan = self.experiments[0].scan

        if "flags" in self.reflections:
            strong_sel = self.reflections.get_flags(self.reflections.flags.strong)
            if strong_sel.count(True) > 0:
                self.reflections = self.reflections.select(strong_sel)
        if "flags" not in self.reflections or strong_sel.count(True) == 0:
            # backwards compatibility for testing
            self.reflections.set_flags(
                flex.size_t_range(len(self.reflections)), self.reflections.flags.strong
            )

        self._setup_symmetry()
        self.d_min = None

        self.setup_indexing()

    @staticmethod
    def from_parameters(
        reflections, experiments, known_crystal_models=None, params=None
    ):

        if params is None:
            params = master_params

        if known_crystal_models is not None:
            from dials.algorithms.indexing.known_orientation import (
                indexer_known_orientation,
            )

            if params.indexing.known_symmetry.space_group is None:
                params.indexing.known_symmetry.space_group = (
                    known_crystal_models[0].get_space_group().info()
                )
            idxr = indexer_known_orientation(
                reflections, experiments, params, known_crystal_models
            )
        else:
            has_stills = False
            has_sweeps = False
            for expt in experiments:
                if (
                    expt.goniometer is None
                    or expt.scan is None
                    or expt.scan.get_oscillation()[1] == 0
                ):
                    if has_sweeps:
                        raise Sorry(
                            "Please provide only stills or only sweeps, not both"
                        )
                    has_stills = True
                else:
                    if has_stills:
                        raise Sorry(
                            "Please provide only stills or only sweeps, not both"
                        )
                    has_sweeps = True
            assert not (has_stills and has_sweeps)
            use_stills_indexer = has_stills

            if not (
                params.indexing.stills.indexer is libtbx.Auto
                or params.indexing.stills.indexer.lower() == "auto"
            ):
                if params.indexing.stills.indexer == "stills":
                    use_stills_indexer = True
                elif params.indexing.stills.indexer == "sweeps":
                    use_stills_indexer = False
                else:
                    assert False

            if params.indexing.basis_vector_combinations.max_refine is libtbx.Auto:
                if use_stills_indexer:
                    params.indexing.basis_vector_combinations.max_refine = 5
                else:
                    params.indexing.basis_vector_combinations.max_refine = 50

            if use_stills_indexer:
                # Ensure the indexer and downstream applications treat this as set of stills
                from dxtbx.imageset import ImageSet  # , MemImageSet

                for experiment in experiments:
                    experiment.imageset = ImageSet(
                        experiment.imageset.data(), experiment.imageset.indices()
                    )
                    # if isinstance(imageset, MemImageSet):
                    #   imageset = MemImageSet(imagesweep._images, imagesweep.indices())
                    # else:
                    #   imageset = ImageSet(imagesweep.reader(), imagesweep.indices())
                    #   imageset._models = imagesweep._models
                    experiment.imageset.set_scan(None)
                    experiment.imageset.set_goniometer(None)
                    experiment.scan = None
                    experiment.goniometer = None

            if params.indexing.method in ("fft1d", "fft3d", "real_space_grid_search"):
                if use_stills_indexer:
                    # do something
                    from dials.algorithms.indexing.stills_indexer import (
                        stills_indexer_basis_vector_search as BasisVectorSearch,
                    )
                else:
                    from dials.algorithms.indexing.lattice_search import (
                        BasisVectorSearch,
                    )

                idxr = BasisVectorSearch(reflections, experiments, params=params)

        return idxr

    def _setup_symmetry(self):
        target_unit_cell = self.params.known_symmetry.unit_cell
        target_space_group = self.params.known_symmetry.space_group
        if target_space_group is not None:
            target_space_group = target_space_group.group()
        self._symmetry_handler = SymmetryHandler(
            unit_cell=target_unit_cell,
            space_group=target_space_group,
            max_delta=self.params.known_symmetry.max_delta,
        )
        return

    def setup_indexing(self):
        reflections_input = self.reflections
        self.reflections = flex.reflection_table()
        for i, expt in enumerate(self.experiments):
            if "imageset_id" not in reflections_input:
                reflections_input["imageset_id"] = reflections_input["id"]
            refl = reflections_input.select(reflections_input["imageset_id"] == i)
            refl.centroid_px_to_mm(expt.detector, expt.scan)
            self.reflections.extend(refl)
        self.filter_reflections_by_scan_range()
        if len(self.reflections) == 0:
            raise Sorry("No reflections left to index!")

        spots_mm = self.reflections
        self.reflections = flex.reflection_table()

        for i, expt in enumerate(self.experiments):
            spots_sel = spots_mm.select(spots_mm["imageset_id"] == i)
            spots_sel.map_centroids_to_reciprocal_space(
                expt.detector, expt.beam, expt.goniometer
            )
            spots_sel["entering"] = self.calculate_entering_flags(
                spots_sel, beam=expt.beam, goniometer=expt.goniometer
            )
            self.reflections.extend(spots_sel)

        try:
            self.find_max_cell()
        except AssertionError as e:
            if "too few spots" in str(e).lower():
                raise Sorry(e)

        if self.params.sigma_phi_deg is not None:
            var_x, var_y, _ = self.reflections["xyzobs.mm.variance"].parts()
            var_phi_rad = flex.double(
                var_x.size(), (math.pi / 180 * self.params.sigma_phi_deg) ** 2
            )
            self.reflections["xyzobs.mm.variance"] = flex.vec3_double(
                var_x, var_y, var_phi_rad
            )

        if self.params.debug:
            self.debug_write_reciprocal_lattice_points_as_pdb()

        self.reflections["id"] = flex.int(len(self.reflections), -1)

    def index(self):

        experiments = ExperimentList()

        had_refinement_error = False
        have_similar_crystal_models = False

        while True:
            if had_refinement_error or have_similar_crystal_models:
                break
            max_lattices = self.params.multiple_lattice_search.max_lattices
            if max_lattices is not None and len(experiments) >= max_lattices:
                break
            if len(experiments) > 0:
                cutoff_fraction = (
                    self.params.multiple_lattice_search.recycle_unindexed_reflections_cutoff
                )
                d_spacings = 1 / self.reflections["rlp"].norms()
                d_min_indexed = flex.min(d_spacings.select(self.indexed_reflections))
                min_reflections_for_indexing = cutoff_fraction * len(
                    self.reflections.select(d_spacings > d_min_indexed)
                )
                crystal_ids = self.reflections.select(d_spacings > d_min_indexed)["id"]
                if (crystal_ids == -1).count(True) < min_reflections_for_indexing:
                    logger.info(
                        "Finish searching for more lattices: %i unindexed reflections remaining."
                        % (min_reflections_for_indexing)
                    )
                    break

            n_lattices_previous_cycle = len(experiments)

            if self.d_min is None:
                self.d_min = self.params.refinement_protocol.d_min_start

            if len(experiments) == 0:
                experiments.extend(self.find_lattices())
            else:
                try:
                    new = self.find_lattices()
                    experiments.extend(new)
                except Sorry:
                    logger.info("Indexing remaining reflections failed")

            if self.params.refinement_protocol.d_min_step is libtbx.Auto:
                n_cycles = self.params.refinement_protocol.n_macro_cycles
                if self.d_min is None or n_cycles == 1:
                    self.params.refinement_protocol.d_min_step = 0
                else:
                    d_spacings = 1 / self.reflections["rlp"].norms()
                    d_min_all = flex.min(d_spacings)
                    self.params.refinement_protocol.d_min_step = (
                        self.d_min - d_min_all
                    ) / (n_cycles - 1)
                    logger.info(
                        "Using d_min_step %.1f"
                        % self.params.refinement_protocol.d_min_step
                    )

            if len(experiments) == 0:
                raise Sorry("No suitable lattice could be found.")
            elif len(experiments) == n_lattices_previous_cycle:
                # no more lattices found
                break

            for i_cycle in range(self.params.refinement_protocol.n_macro_cycles):
                if (
                    i_cycle > 0
                    and self.d_min is not None
                    and self.params.refinement_protocol.d_min_step > 0
                ):
                    d_min = self.d_min - self.params.refinement_protocol.d_min_step
                    d_min = max(d_min, 0)
                    d_min = max(d_min, self.params.refinement_protocol.d_min_final)
                    if d_min >= 0:
                        self.d_min = d_min
                        logger.info("Increasing resolution to %.2f Angstrom" % d_min)

                # reset reflection lattice flags
                # the lattice a given reflection belongs to: a value of -1 indicates
                # that a reflection doesn't belong to any lattice so far
                self.reflections["id"] = flex.int(len(self.reflections), -1)

                self.index_reflections(experiments, self.reflections)

                if i_cycle == 0 and self.params.known_symmetry.space_group is not None:
                    # now apply the space group symmetry only after the first indexing
                    # need to make sure that the symmetrized orientation is similar to the P1 model
                    for i_cryst, cryst in enumerate(experiments.crystals()):
                        if i_cryst >= n_lattices_previous_cycle:
                            new_cryst, cb_op_to_primitive = self._symmetry_handler.apply_symmetry(
                                cryst
                            )
                            if self._symmetry_handler.cb_op_primitive_inp is not None:
                                new_cryst = new_cryst.change_basis(
                                    self._symmetry_handler.cb_op_primitive_inp
                                )
                            cryst.update(new_cryst)
                            cryst.set_space_group(
                                self.params.known_symmetry.space_group.group()
                            )
                            for i_expt, expt in enumerate(experiments):
                                if expt.crystal is not cryst:
                                    continue
                                if not cb_op_to_primitive.is_identity_op():
                                    miller_indices = self.reflections[
                                        "miller_index"
                                    ].select(self.reflections["id"] == i_expt)
                                    miller_indices = cb_op_to_primitive.apply(
                                        miller_indices
                                    )
                                    self.reflections["miller_index"].set_selected(
                                        self.reflections["id"] == i_expt, miller_indices
                                    )
                                if (
                                    self._symmetry_handler.cb_op_primitive_inp
                                    is not None
                                ):
                                    miller_indices = self.reflections[
                                        "miller_index"
                                    ].select(self.reflections["id"] == i_expt)
                                    miller_indices = self._symmetry_handler.cb_op_primitive_inp.apply(
                                        miller_indices
                                    )
                                    self.reflections["miller_index"].set_selected(
                                        self.reflections["id"] == i_expt, miller_indices
                                    )
                    logger.info("\nIndexed crystal models:")
                    self.show_experiments(
                        experiments, self.reflections, d_min=self.d_min
                    )

                if len(experiments) > 1:
                    cryst_b = experiments.crystals()[-1]
                    have_similar_crystal_models = False
                    for i_a, cryst_a in enumerate(experiments.crystals()[:-1]):
                        R_ab, axis, angle, cb_op_ab = difference_rotation_matrix_axis_angle(
                            cryst_a, cryst_b
                        )
                        min_angle = (
                            self.params.multiple_lattice_search.minimum_angular_separation
                        )
                        if abs(angle) < min_angle:  # degrees
                            logger.info(
                                "Crystal models too similar, rejecting crystal %i:"
                                % (len(experiments))
                            )
                            logger.info(
                                "Rotation matrix to transform crystal %i to crystal %i"
                                % (i_a + 1, len(experiments))
                            )
                            logger.info(R_ab)
                            logger.info(
                                "Rotation of %.3f degrees" % angle
                                + " about axis (%.3f, %.3f, %.3f)" % axis
                            )
                            # show_rotation_matrix_differences([cryst_a, cryst_b])
                            have_similar_crystal_models = True
                            del experiments[-1]
                            break
                    if have_similar_crystal_models:
                        break

                logger.info("")
                logger.info("#" * 80)
                logger.info("Starting refinement (macro-cycle %i)" % (i_cycle + 1))
                logger.info("#" * 80)
                logger.info("")
                self.indexed_reflections = self.reflections["id"] > -1

                sel = flex.bool(len(self.reflections), False)
                lengths = 1 / self.reflections["rlp"].norms()
                if self.d_min is not None:
                    isel = (lengths <= self.d_min).iselection()
                    sel.set_selected(isel, True)
                sel.set_selected(self.reflections["id"] == -1, True)
                self.reflections.unset_flags(sel, self.reflections.flags.indexed)
                self.unindexed_reflections = self.reflections.select(sel)

                reflections_for_refinement = self.reflections.select(
                    self.indexed_reflections
                )
                if self.params.refinement_protocol.mode == "repredict_only":
                    refined_experiments, refined_reflections = (
                        experiments,
                        reflections_for_refinement,
                    )
                    from dials.algorithms.refinement.prediction.managed_predictors import (
                        ExperimentsPredictorFactory,
                    )

                    ref_predictor = ExperimentsPredictorFactory.from_experiments(
                        experiments,
                        spherical_relp=self.all_params.refinement.parameterisation.spherical_relp_model,
                    )
                    ref_predictor(refined_reflections)
                else:
                    try:
                        refined_experiments, refined_reflections = self.refine(
                            experiments, reflections_for_refinement
                        )
                    except Sorry as e:
                        if len(experiments) == 1:
                            raise
                        had_refinement_error = True
                        logger.info("Refinement failed:")
                        logger.info(e)
                        del experiments[-1]
                        break

                # sanity check for unrealistic unit cell volume increase during refinement
                # usually this indicates too many parameters are being refined given the
                # number of observations provided.
                if (
                    not self.params.refinement_protocol.disable_unit_cell_volume_sanity_check
                ):
                    for orig_expt, refined_expt in zip(
                        experiments, refined_experiments
                    ):
                        uc1 = orig_expt.crystal.get_unit_cell()
                        uc2 = refined_expt.crystal.get_unit_cell()
                        volume_change = abs(uc1.volume() - uc2.volume()) / uc1.volume()
                        cutoff = 0.5
                        if volume_change > cutoff:
                            msg = "\n".join(
                                (
                                    "Unrealistic unit cell volume increase during refinement of %.1f%%.",
                                    "Please try refining fewer parameters, either by enforcing symmetry",
                                    "constraints (space_group=) and/or disabling experimental geometry",
                                    "refinement (detector.fix=all and beam.fix=all). To disable this",
                                    "sanity check set disable_unit_cell_volume_sanity_check=True.",
                                )
                            ) % (100 * volume_change)
                            raise Sorry(msg)

                self.refined_reflections = refined_reflections
                self.refined_reflections.unset_flags(
                    self.refined_reflections["id"] < 0,
                    self.refined_reflections.flags.indexed,
                )

                for i, expt in enumerate(self.experiments):
                    ref_sel = self.refined_reflections.select(
                        self.refined_reflections["imageset_id"] == i
                    )
                    ref_sel = ref_sel.select(ref_sel["id"] >= 0)
                    for i_expt in set(ref_sel["id"]):
                        refined_expt = refined_experiments[i_expt]
                        expt.detector = refined_expt.detector
                        expt.beam = refined_expt.beam
                        expt.goniometer = refined_expt.goniometer
                        expt.scan = refined_expt.scan
                        refined_expt.imageset = expt.imageset

                if not (
                    self.all_params.refinement.parameterisation.beam.fix == "all"
                    and self.all_params.refinement.parameterisation.detector.fix
                    == "all"
                ):
                    # Experimental geometry may have changed - re-map centroids to
                    # reciprocal space

                    spots_mm = self.reflections
                    self.reflections = flex.reflection_table()
                    for i, expt in enumerate(self.experiments):
                        spots_sel = spots_mm.select(spots_mm["imageset_id"] == i)
                        spots_sel.map_centroids_to_reciprocal_space(
                            expt.detector, expt.beam, expt.goniometer
                        )
                        self.reflections.extend(spots_sel)

                # update for next cycle
                experiments = refined_experiments
                self.refined_experiments = refined_experiments

                logger.info("\nRefined crystal models:")
                self.show_experiments(
                    self.refined_experiments, self.reflections, d_min=self.d_min
                )

                if (
                    i_cycle >= 2
                    and self.d_min == self.params.refinement_protocol.d_min_final
                ):
                    logger.info("Target d_min_final reached: finished with refinement")
                    break

        if not "refined_experiments" in locals():
            raise Sorry("None of the experiments could refine.")

        if len(self.refined_experiments) > 1:
            from dials.algorithms.indexing.compare_orientation_matrices import (
                rotation_matrix_differences,
            )

            logger.info(
                rotation_matrix_differences(self.refined_experiments.crystals())
            )

        self.refined_reflections["xyzcal.px"] = flex.vec3_double(
            len(self.refined_reflections)
        )
        for i, expt in enumerate(self.experiments):
            imgset_sel = self.refined_reflections["imageset_id"] == i
            # set xyzcal.px field in self.refined_reflections
            refined_reflections = self.refined_reflections.select(imgset_sel)
            panel_numbers = flex.size_t(refined_reflections["panel"])
            xyzcal_mm = refined_reflections["xyzcal.mm"]
            x_mm, y_mm, z_rad = xyzcal_mm.parts()
            xy_cal_mm = flex.vec2_double(x_mm, y_mm)
            xy_cal_px = flex.vec2_double(len(xy_cal_mm))
            for i_panel in range(len(expt.detector)):
                panel = expt.detector[i_panel]
                sel = panel_numbers == i_panel
                isel = sel.iselection()
                ref_panel = refined_reflections.select(panel_numbers == i_panel)
                xy_cal_px.set_selected(
                    sel, panel.millimeter_to_pixel(xy_cal_mm.select(sel))
                )
            x_px, y_px = xy_cal_px.parts()
            if expt.scan is not None:
                z_px = expt.scan.get_array_index_from_angle(z_rad, deg=False)
            else:
                # must be a still image, z centroid not meaningful
                z_px = z_rad
            xyzcal_px = flex.vec3_double(x_px, y_px, z_px)
            self.refined_reflections["xyzcal.px"].set_selected(imgset_sel, xyzcal_px)

    def show_experiments(self, experiments, reflections, d_min=None):
        if d_min is not None:
            reciprocal_lattice_points = reflections["rlp"]
            d_spacings = 1 / reciprocal_lattice_points.norms()
            reflections = reflections.select(d_spacings > d_min)
        for i_expt, expt in enumerate(experiments):
            logger.info(
                "model %i (%i reflections):"
                % (i_expt + 1, (reflections["id"] == i_expt).count(True))
            )
            logger.info(expt.crystal)

        indexed_flags = reflections.get_flags(reflections.flags.indexed)
        imageset_id = reflections["imageset_id"]
        rows = [["Imageset", "# indexed", "# unindexed", "% indexed"]]
        for i in range(flex.max(imageset_id) + 1):
            imageset_indexed_flags = indexed_flags.select(imageset_id == i)
            indexed_count = imageset_indexed_flags.count(True)
            unindexed_count = imageset_indexed_flags.count(False)
            rows.append(
                [
                    str(i),
                    str(indexed_count),
                    str(unindexed_count),
                    "{:.1%}".format(indexed_count / (indexed_count + unindexed_count)),
                ]
            )
        from libtbx import table_utils

        logger.info(
            table_utils.format(rows, has_header=True, prefix="| ", postfix=" |")
        )

    def find_max_cell(self):
        params = self.params.max_cell_estimation
        if self.params.max_cell is libtbx.Auto:
            if self.params.known_symmetry.unit_cell is not None:
                uc_params = (
                    self._symmetry_handler.target_symmetry_primitive.unit_cell().parameters()
                )
                self.params.max_cell = params.multiplier * max(uc_params[:3])
                logger.info("Using max_cell: %.1f Angstrom" % (self.params.max_cell))
            else:
                self.params.max_cell = find_max_cell(
                    self.reflections,
                    max_cell_multiplier=params.multiplier,
                    step_size=params.step_size,
                    nearest_neighbor_percentile=params.nearest_neighbor_percentile,
                    histogram_binning=params.histogram_binning,
                    nn_per_bin=params.nn_per_bin,
                    max_height_fraction=params.max_height_fraction,
                    filter_ice=params.filter_ice,
                    filter_overlaps=params.filter_overlaps,
                    overlaps_border=params.overlaps_border,
                ).max_cell
                logger.info("Found max_cell: %.1f Angstrom" % (self.params.max_cell))

    def filter_reflections_by_scan_range(self):
        if len(self.params.scan_range):
            self.reflections = filter_reflections_by_scan_range(
                self.reflections, self.params.scan_range
            )

    @staticmethod
    def calculate_entering_flags(reflections, beam, goniometer):
        if goniometer is None:
            return flex.bool(len(reflections), False)
        axis = matrix.col(goniometer.get_rotation_axis())
        s0 = matrix.col(beam.get_s0())
        # calculate a unit vector normal to the spindle-beam plane for this
        # experiment, such that the vector placed at the centre of the Ewald sphere
        # points to the hemisphere in which reflections cross from inside to outside
        # of the sphere (reflections are exiting). NB this vector is in +ve Y
        # direction when using imgCIF coordinate frame.
        vec = s0.cross(axis)
        entering = reflections["s1"].dot(vec) < 0.0
        return entering

    def index_reflections(self, experiments, reflections):
        self._assign_indices(reflections, experiments, d_min=self.d_min)
        if self.hkl_offset is not None and self.hkl_offset != (0, 0, 0):
            reflections["miller_index"] = apply_hkl_offset(
                reflections["miller_index"], self.hkl_offset
            )
            self.hkl_offset = None

    def refine(self, experiments, reflections):
        from dials.algorithms.indexing.refinement import refine

        refiner, refined, outliers = refine(
            self.all_params,
            reflections,
            experiments,
            verbosity=self.params.refinement_protocol.verbosity,
            debug_plots=self.params.debug_plots,
        )
        if outliers is not None:
            reflections["id"].set_selected(outliers, -1)
        predicted = refiner.predict_for_indexed()
        verbosity = self.params.refinement_protocol.verbosity
        reflections["xyzcal.mm"] = predicted["xyzcal.mm"]
        reflections["entering"] = predicted["entering"]
        reflections.unset_flags(
            flex.bool(len(reflections), True), reflections.flags.centroid_outlier
        )
        assert (
            reflections.get_flags(reflections.flags.centroid_outlier).count(True) == 0
        )
        reflections.set_flags(
            predicted.get_flags(predicted.flags.centroid_outlier),
            reflections.flags.centroid_outlier,
        )
        reflections.set_flags(
            refiner.selection_used_for_refinement(),
            reflections.flags.used_in_refinement,
        )
        return refiner.get_experiments(), reflections

    def debug_write_reciprocal_lattice_points_as_pdb(
        self, file_name="reciprocal_lattice.pdb"
    ):
        from cctbx import crystal, xray

        cs = crystal.symmetry(
            unit_cell=(1000, 1000, 1000, 90, 90, 90), space_group="P1"
        )
        for i_panel in range(len(self.experiments[0].detector)):
            if len(self.experiments[0].detector) > 1:
                file_name = "reciprocal_lattice_%i.pdb" % i_panel
            with open(file_name, "wb") as f:
                xs = xray.structure(crystal_symmetry=cs)
                reflections = self.reflections.select(
                    self.reflections["panel"] == i_panel
                )
                for site in reflections["rlp"]:
                    xs.add_scatterer(xray.scatterer("C", site=site))
                xs.sites_mod_short()
                f.write(xs.as_pdb_file())

    def debug_write_ccp4_map(self, map_data, file_name):
        from iotbx import ccp4_map

        gridding_first = (0, 0, 0)
        gridding_last = map_data.all()
        labels = ["cctbx.miller.fft_map"]
        ccp4_map.write_ccp4_map(
            file_name=file_name,
            unit_cell=self.fft_cell,
            space_group=sgtbx.space_group("P1"),
            gridding_first=gridding_first,
            gridding_last=gridding_last,
            map_data=map_data,
            labels=flex.std_string(labels),
        )

    def export_as_json(
        self, experiments, file_name="indexed_experiments.json", compact=False
    ):
        from dxtbx.serialize import dump

        assert experiments.is_consistent()
        dump.experiment_list(experiments, file_name)

    def export_reflections(self, reflections, file_name="reflections.pickle"):
        reflections.as_pickle(file_name)

    def find_lattices(self):
        raise NotImplementedError()


def optimise_basis_vectors(reciprocal_lattice_points, vectors):
    optimised = flex.vec3_double()
    for vector in vectors:
        minimised = basis_vector_minimser(reciprocal_lattice_points, vector)
        optimised.append(tuple(minimised.x))
    return optimised


from scitbx import lbfgs

# Optimise the initial basis vectors as per equation 11.4.3.4 of
# Otwinowski et al, International Tables Vol. F, chapter 11.4 pp. 282-295
class basis_vector_target(object):
    def __init__(self, reciprocal_lattice_points):
        self.reciprocal_lattice_points = reciprocal_lattice_points
        self._xyz_parts = self.reciprocal_lattice_points.parts()

    def compute_functional_and_gradients(self, vector):
        assert len(vector) == 3
        two_pi_S_dot_v = 2 * math.pi * self.reciprocal_lattice_points.dot(vector)
        f = -flex.sum(flex.cos(two_pi_S_dot_v))
        sin_part = flex.sin(two_pi_S_dot_v)
        g = flex.double(
            [flex.sum(2 * math.pi * self._xyz_parts[i] * sin_part) for i in range(3)]
        )
        return f, g


class basis_vector_minimser(object):
    def __init__(
        self,
        reciprocal_lattice_points,
        vector,
        lbfgs_termination_params=None,
        lbfgs_core_params=lbfgs.core_parameters(m=20),
    ):
        self.reciprocal_lattice_points = reciprocal_lattice_points
        if not isinstance(vector, flex.double):
            self.x = flex.double(vector)
        else:
            self.x = vector.deep_copy()
        self.n = len(self.x)
        assert self.n == 3
        self.target = basis_vector_target(self.reciprocal_lattice_points)
        self.minimizer = lbfgs.run(
            target_evaluator=self,
            termination_params=lbfgs_termination_params,
            core_params=lbfgs_core_params,
        )
        # print "number of iterations:", self.minimizer.iter()

    def compute_functional_and_gradients(self):
        f, g = self.target.compute_functional_and_gradients(tuple(self.x))
        # g_fd = _gradient_fd(self.target, tuple(self.x))
        # from libtbx.test_utils import approx_equal
        # assert approx_equal(g, g_fd, eps=1e-3)
        return f, g

    def callback_after_step(self, minimizer):
        # print tuple(self.x)
        return


def _gradient_fd(target, vector, eps=1e-6):
    grads = []
    for i in range(len(vector)):
        v = list(vector)
        v[i] -= eps
        tm, _ = target.compute_functional_and_gradients(v)
        v[i] += 2 * eps
        tp, _ = target.compute_functional_and_gradients(v)
        grads.append((tp - tm) / (2 * eps))
    return grads


def reject_weight_outliers_selection(reflections, sigma_cutoff=5):
    from scitbx.math import basic_statistics

    variances = flex.vec3_double([r.centroid_variance for r in reflections])
    selection = None
    for v in variances.parts():
        w = 1 / v
        ln_w = flex.log(w)
        stats = basic_statistics(ln_w)
        sel = ln_w < (
            sigma_cutoff * stats.bias_corrected_standard_deviation + stats.mean
        )
        if selection is None:
            selection = sel
        else:
            selection &= sel
    return selection


def hist_outline(hist):

    step_size = hist.slot_width()
    half_step_size = 0.5 * step_size
    n_slots = len(hist.slots())

    bins = flex.double(n_slots * 2 + 2, 0)
    data = flex.double(n_slots * 2 + 2, 0)
    for i in range(n_slots):
        bins[2 * i + 1] = hist.slot_centers()[i] - half_step_size
        bins[2 * i + 2] = hist.slot_centers()[i] + half_step_size
        data[2 * i + 1] = hist.slots()[i]
        data[2 * i + 2] = hist.slots()[i]

    bins[0] = bins[1] - step_size
    bins[-1] = bins[-2] + step_size
    data[0] = 0
    data[-1] = 0

    return (bins, data)


def plot_centroid_weights_histograms(reflections, n_slots=50):
    from matplotlib import pyplot

    variances = flex.vec3_double([r.centroid_variance for r in reflections])
    vx, vy, vz = variances.parts()
    wx = 1 / vx
    wy = 1 / vy
    wz = 1 / vz
    # hx = flex.histogram(vx, n_slots=n_slots)
    # hy = flex.histogram(vy, n_slots=n_slots)
    # hz = flex.histogram(vz, n_slots=n_slots)
    wx = flex.log(wx)
    wy = flex.log(wy)
    wz = flex.log(wz)
    hx = flex.histogram(wx, n_slots=n_slots)
    hy = flex.histogram(wy, n_slots=n_slots)
    hz = flex.histogram(wz, n_slots=n_slots)
    fig = pyplot.figure()

    # outliers = reflections.select(wx > 50)
    # for refl in outliers:
    # print refl

    for i, h in enumerate([hx, hy, hz]):
        ax = fig.add_subplot(311 + i)

        slots = h.slots().as_double()
        bins, data = hist_outline(h)
        log_scale = True
        if log_scale:
            data.set_selected(
                data == 0, 0.1
            )  # otherwise lines don't get drawn when we have some empty bins
            ax.set_yscale("log")
        ax.plot(bins, data, "-k", linewidth=2)
        # pyplot.suptitle(title)
        data_min = min([slot.low_cutoff for slot in h.slot_infos() if slot.n > 0])
        data_max = max([slot.low_cutoff for slot in h.slot_infos() if slot.n > 0])
        ax.set_xlim(data_min, data_max + h.slot_width())
    pyplot.show()


def apply_hkl_offset(indices, offset):
    h, k, l = indices.as_vec3_double().parts()
    h += offset[0]
    k += offset[1]
    l += offset[2]
    return flex.miller_index(h.iround(), k.iround(), l.iround())
