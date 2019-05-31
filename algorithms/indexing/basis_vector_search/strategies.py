from __future__ import absolute_import, division
from __future__ import print_function

import abc
import math
import logging

import libtbx
from libtbx.utils import Sorry
from scitbx.array_family import flex
from scitbx import fftpack
from scitbx import matrix
from cctbx import crystal, uctbx, xray

logger = logging.getLogger(__name__)


class strategy(object):

    __metaclass__ = abc.ABCMeta

    def __init__(self, max_cell):
        self._max_cell = max_cell

    @abc.abstractmethod
    def find_basis_vectors(self, reciprocal_lattice_vectors):
        pass


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


class fft1d(strategy):
    def __init__(self, max_cell, characteristic_grid=None):
        super(fft1d, self).__init__(max_cell)
        self._characteristic_grid = characteristic_grid

    def find_basis_vectors(self, reciprocal_lattice_vectors):
        from rstbx.phil.phil_preferences import indexing_api_defs
        import iotbx.phil

        hardcoded_phil = iotbx.phil.parse(input_string=indexing_api_defs).extract()

        # Spot_positions: Centroid positions for spotfinder spots, in pixels
        # Return value: Corrected for parallax, converted to mm

        # derive a max_cell from mm spots
        # derive a grid sampling from spots

        from rstbx.indexing_api.lattice import DPS_primitive_lattice

        # max_cell: max possible cell in Angstroms; set to None, determine from data
        # recommended_grid_sampling_rad: grid sampling in radians; guess for now

        DPS = DPS_primitive_lattice(
            max_cell=self._max_cell,
            recommended_grid_sampling_rad=self._characteristic_grid,
            horizon_phil=hardcoded_phil,
        )

        # transform input into what Nick needs
        # i.e., construct a flex.vec3 double consisting of mm spots, phi in degrees
        DPS.index(reciprocal_space_vectors=reciprocal_lattice_vectors)
        solutions = DPS.getSolutions()
        candidate_basis_vectors = [matrix.col(s.bvec()) for s in solutions]
        return candidate_basis_vectors


class fft3d(strategy):
    def __init__(
        self,
        max_cell,
        n_points,
        d_min=libtbx.Auto,
        b_iso=libtbx.Auto,
        rmsd_cutoff=15,
        peak_volume_cutoff=0.15,
        min_cell=3,
    ):
        super(fft3d, self).__init__(max_cell)
        self._n_points = n_points
        self._gridding = fftpack.adjust_gridding_triple(
            (self._n_points, self._n_points, self._n_points), max_prime=5
        )
        self._n_points = self._gridding[0]
        self._d_min = d_min
        self._b_iso = b_iso
        self._rmsd_cutoff = rmsd_cutoff
        self._peak_volume_cutoff = peak_volume_cutoff
        self._min_cell = min_cell

    def find_basis_vectors(self, reciprocal_lattice_vectors):

        if self._d_min is libtbx.Auto:
            # rough calculation of suitable d_min based on max cell
            # see also Campbell, J. (1998). J. Appl. Cryst., 31(3), 407-413.
            # fft_cell should be greater than twice max_cell, so say:
            #   fft_cell = 2.5 * max_cell
            # then:
            #   fft_cell = n_points * d_min/2
            #   2.5 * max_cell = n_points * d_min/2
            # a little bit of rearrangement:
            #   d_min = 5 * max_cell/n_points

            max_cell = self._max_cell
            d_min = 5 * max_cell / self._n_points
            d_spacings = 1 / reciprocal_lattice_vectors.norms()
            d_min = max(d_min, min(d_spacings))
            logger.info("Setting d_min: %.2f" % d_min)
        else:
            d_min = self._d_min

        grid_real = self._fft(reciprocal_lattice_vectors, d_min)
        self.sites, self.volumes = self.find_peaks(grid_real, d_min)

        # hijack the xray.structure class to facilitate calculation of distances

        self.crystal_symmetry = crystal.symmetry(
            unit_cell=self._fft_cell, space_group_symbol="P1"
        )
        xs = xray.structure(crystal_symmetry=self.crystal_symmetry)
        for i, site in enumerate(self.sites):
            xs.add_scatterer(xray.scatterer("C%i" % i, site=site))

        xs = xs.sites_mod_short()
        sites_cart = xs.sites_cart()
        lengths = flex.double([matrix.col(sc).length() for sc in sites_cart])
        perm = flex.sort_permutation(lengths)
        xs = xs.select(perm)
        volumes = self.volumes.select(perm)

        sites_frac = xs.sites_frac()
        vectors = xs.sites_cart()
        norms = vectors.norms()
        sel = (norms > self._min_cell) & (norms < (2 * self._max_cell))
        vectors = vectors.select(sel)
        vectors = [matrix.col(v) for v in vectors]
        volumes = volumes.select(sel)

        # XXX loop over these vectors and sort into groups similar to further down
        # group similar angle and lengths, also catch integer multiples of vectors

        vector_groups = []
        relative_length_tolerance = 0.1
        angle_tolerance = 5  # degrees

        orth = self._fft_cell.orthogonalize
        for v, volume in zip(vectors, volumes):
            length = v.length()
            if length < self._min_cell or length > (2 * self._max_cell):
                continue
            matched_group = False
            for group in vector_groups:
                mean_v = group.mean()
                mean_v_length = mean_v.length()
                if (
                    abs(mean_v_length - length) / max(mean_v_length, length)
                    < relative_length_tolerance
                ):
                    angle = mean_v.angle(v, deg=True)
                    if angle < angle_tolerance:
                        group.append(v, length, volume)
                        matched_group = True
                        break
                    elif abs(180 - angle) < angle_tolerance:
                        group.append(-v, length, volume)
                        matched_group = True
                        break
            if not matched_group:
                group = vector_group()
                group.append(v, length, volume)
                vector_groups.append(group)

        vectors = [g.mean() for g in vector_groups]
        volumes = flex.double(max(g.volumes) for g in vector_groups)

        # sort by peak size
        perm = flex.sort_permutation(volumes, reverse=True)
        volumes = volumes.select(perm)
        vectors = [vectors[i] for i in perm]

        for i, (v, volume) in enumerate(zip(vectors, volumes)):
            logger.debug("%s %s %s" % (i, v.length(), volume))

        # sort by length
        lengths = flex.double(v.length() for v in vectors)
        perm = flex.sort_permutation(lengths)

        # exclude vectors that are (approximately) integer multiples of a shorter
        # vector
        unique_vectors = []
        unique_volumes = flex.double()
        for p in perm:
            v = vectors[p]
            is_unique = True
            for i, v_u in enumerate(unique_vectors):
                if (unique_volumes[i] > volumes[p]) and is_approximate_integer_multiple(
                    v_u, v
                ):
                    logger.debug(
                        "rejecting %s: integer multiple of %s"
                        % (v.length(), v_u.length())
                    )
                    is_unique = False
                    break
            if is_unique:
                unique_vectors.append(v)
                unique_volumes.append(volumes[p])

        # re-sort by peak volume
        perm = flex.sort_permutation(unique_volumes, reverse=True)
        vectors = [unique_vectors[i] for i in perm]
        volumes = unique_volumes.select(perm)

        # for i, (v, volume) in enumerate(zip(vectors, volumes)):
        # logger.debug("%s %s %s" %(i, v.length(), volume))

        self.candidate_basis_vectors = vectors
        return self.candidate_basis_vectors

    def _fft(self, reciprocal_lattice_vectors, d_min):

        reciprocal_space_grid = self._map_centroids_to_reciprocal_space_grid(
            reciprocal_lattice_vectors, d_min
        )

        logger.info(
            "Number of centroids used: %i" % ((reciprocal_space_grid > 0).count(True))
        )

        # gb_to_bytes = 1073741824
        # bytes_to_gb = 1/gb_to_bytes
        # (128**3)*8*2*bytes_to_gb
        # 0.03125
        # (256**3)*8*2*bytes_to_gb
        # 0.25
        # (512**3)*8*2*bytes_to_gb
        # 2.0

        fft = fftpack.complex_to_complex_3d(self._gridding)
        grid_complex = flex.complex_double(
            reals=reciprocal_space_grid,
            imags=flex.double(reciprocal_space_grid.size(), 0),
        )
        grid_transformed = fft.forward(grid_complex)
        grid_real = flex.pow2(flex.real(grid_transformed))
        del grid_transformed

        return grid_real

    def _map_centroids_to_reciprocal_space_grid(
        self, reciprocal_lattice_vectors, d_min
    ):
        n_points = self._gridding[0]
        rlgrid = 2 / (d_min * n_points)

        logger.info("FFT gridding: (%i,%i,%i)" % self._gridding)

        grid = flex.double(flex.grid(self._gridding), 0)

        if self._b_iso is libtbx.Auto:
            self._b_iso = -4 * d_min ** 2 * math.log(0.05)
            logger.debug("Setting b_iso = %.1f" % self._b_iso)
        # self._b_iso = 0
        selection = flex.bool(reciprocal_lattice_vectors.size(), True)
        from dials.algorithms.indexing import map_centroids_to_reciprocal_space_grid

        map_centroids_to_reciprocal_space_grid(
            grid,
            reciprocal_lattice_vectors,
            selection,  # do we really need this?
            d_min,
            b_iso=self._b_iso,
        )
        return grid

    def find_peaks(self, grid_real, d_min):
        grid_real_binary = grid_real.deep_copy()
        rmsd = math.sqrt(
            flex.mean(
                flex.pow2(
                    grid_real_binary.as_1d() - flex.mean(grid_real_binary.as_1d())
                )
            )
        )
        grid_real_binary.set_selected(grid_real_binary < (self._rmsd_cutoff) * rmsd, 0)
        grid_real_binary.as_1d().set_selected(grid_real_binary.as_1d() > 0, 1)
        grid_real_binary = grid_real_binary.iround()
        from cctbx import masks

        # real space FFT grid dimensions
        cell_lengths = [self._n_points * d_min / 2 for i in range(3)]
        self._fft_cell = uctbx.unit_cell(cell_lengths + [90] * 3)

        flood_fill = masks.flood_fill(grid_real_binary, self._fft_cell)
        if flood_fill.n_voids() < 4:
            # Require at least peak at origin and one peak for each basis vector
            raise Sorry(
                "Indexing failed: fft3d peak search failed to find sufficient number of peaks."
            )

        # the peak at the origin might have a significantly larger volume than the
        # rest so exclude any anomalously large peaks from determining minimum volume
        from scitbx.math import five_number_summary

        outliers = flex.bool(flood_fill.n_voids(), False)
        grid_points_per_void = flood_fill.grid_points_per_void()
        min_x, q1_x, med_x, q3_x, max_x = five_number_summary(grid_points_per_void)
        iqr_multiplier = 5
        iqr_x = q3_x - q1_x
        cut_x = iqr_multiplier * iqr_x
        outliers.set_selected(grid_points_per_void.as_double() > (q3_x + cut_x), True)
        # print q3_x + cut_x, outliers.count(True)
        isel = (
            grid_points_per_void
            > int(
                self._peak_volume_cutoff
                * flex.max(grid_points_per_void.select(~outliers))
            )
        ).iselection()

        sites = flood_fill.centres_of_mass_frac().select(isel)
        volumes = flood_fill.grid_points_per_void().select(isel)
        return sites, volumes


class real_space_grid_search(strategy):
    def __init__(self, max_cell, target_unit_cell, characteristic_grid=0.02):
        super(real_space_grid_search, self).__init__(max_cell)
        self._target_unit_cell = target_unit_cell
        self._characteristic_grid = characteristic_grid

    def find_basis_vectors(self, reciprocal_lattice_vectors):

        logger.info("Indexing from %i reflections" % len(reciprocal_lattice_vectors))

        def compute_functional(vector):
            two_pi_S_dot_v = 2 * math.pi * reciprocal_lattice_vectors.dot(vector)
            return flex.sum(flex.cos(two_pi_S_dot_v))

        from rstbx.array_family import (
            flex,
        )  # required to load scitbx::af::shared<rstbx::Direction> to_python converter
        from rstbx.dps_core import SimpleSamplerTool

        SST = SimpleSamplerTool(self._characteristic_grid)
        SST.construct_hemisphere_grid(SST.incr)
        cell_dimensions = self._target_unit_cell.parameters()[:3]
        unique_cell_dimensions = set(cell_dimensions)
        logger.info(
            "Number of search vectors: %i"
            % (len(SST.angles) * len(unique_cell_dimensions))
        )
        vectors = flex.vec3_double()
        function_values = flex.double()
        for i, direction in enumerate(SST.angles):
            for l in unique_cell_dimensions:
                v = matrix.col(direction.dvec) * l
                f = compute_functional(v.elems)
                vectors.append(v.elems)
                function_values.append(f)

        perm = flex.sort_permutation(function_values, reverse=True)
        vectors = vectors.select(perm)
        function_values = function_values.select(perm)

        unique_vectors = []
        i = 0
        while len(unique_vectors) < 30:
            v = matrix.col(vectors[i])
            is_unique = True
            if i > 0:
                for v_u in unique_vectors:
                    if v.length() < v_u.length():
                        if is_approximate_integer_multiple(v, v_u):
                            is_unique = False
                            break
                    elif is_approximate_integer_multiple(v_u, v):
                        is_unique = False
                        break
            if is_unique:
                unique_vectors.append(v)
            i += 1

        for i in range(30):
            v = matrix.col(vectors[i])
            logger.debug(
                "%s %s %s" % (str(v.elems), str(v.length()), str(function_values[i]))
            )

        logger.info("Number of unique vectors: %i" % len(unique_vectors))

        for i in range(len(unique_vectors)):
            logger.debug(
                "%s %s %s"
                % (
                    str(compute_functional(unique_vectors[i].elems)),
                    str(unique_vectors[i].length()),
                    str(unique_vectors[i].elems),
                )
            )

        return unique_vectors
