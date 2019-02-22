#!/usr/bin/env python
# -*- mode: python; coding: utf-8; indent-tabs-mode: nil; python-indent: 2 -*-
#
# dials.algorithms.indexing.fft3d.py
#
#  Copyright (C) 2014 Diamond Light Source
#
#  Author: Richard Gildea
#
#  This code is distributed under the BSD license, a copy of which is
#  included in the root directory of this package.

from __future__ import absolute_import, division
from __future__ import print_function

from scitbx.array_family import flex
from dxtbx.model.experiment_list import Experiment, ExperimentList
from dials.algorithms.indexing.indexer import indexer_base
from dials.algorithms.indexing.basis_vector_search import strategies

import logging

logger = logging.getLogger(__name__)


class indexer_fft3d(indexer_base):
    def __init__(self, reflections, experiments, params):
        super(indexer_fft3d, self).__init__(reflections, experiments, params)
        self._basis_vector_search_strategy = strategies.fft3d(
            self.params.max_cell,
            self.params.fft3d.reciprocal_space_grid.n_points,
            d_min=self.params.fft3d.reciprocal_space_grid.d_min,
            b_iso=self.params.b_iso,
            rmsd_cutoff=self.params.rmsd_cutoff,
            peak_volume_cutoff=self.params.fft3d.peak_volume_cutoff,
            min_cell=self.params.min_cell,
        )

    def find_candidate_basis_vectors(self):
        self.d_min = self.params.refinement_protocol.d_min_start
        sel = self.reflections["id"] == -1
        if self.d_min is not None:
            sel &= 1 / self.reflections["rlp"].norms() > self.d_min
        reflections = self.reflections.select(sel)
        self.candidate_basis_vectors, used_in_indexing = self._basis_vector_search_strategy.find_basis_vectors(
            reflections["rlp"]
        )
        used_in_indexing = sel.iselection().select(used_in_indexing)
        if self.d_min is None:
            self.d_min = flex.min(
                1 / self.reflections["rlp"].select(used_in_indexing).norms()
            )

        self.debug_show_candidate_basis_vectors()
        if self.params.debug_plots:
            self.debug_plot_candidate_basis_vectors()

        return self.candidate_basis_vectors

    def find_lattices(self):
        self.find_candidate_basis_vectors()
        self.candidate_crystal_models = self.find_candidate_orientation_matrices(
            self.candidate_basis_vectors
        )
        crystal_model, n_indexed = self.choose_best_orientation_matrix(
            self.candidate_crystal_models
        )
        if crystal_model is not None:
            crystal_models = [crystal_model]
        else:
            crystal_models = []
        experiments = ExperimentList()
        for cm in crystal_models:
            for expt in self.experiments:
                experiments.append(
                    Experiment(
                        imageset=expt.imageset,
                        beam=expt.beam,
                        detector=expt.detector,
                        goniometer=expt.goniometer,
                        scan=expt.scan,
                        crystal=cm,
                    )
                )
        return experiments
