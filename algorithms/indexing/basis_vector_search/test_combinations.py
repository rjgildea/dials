from __future__ import absolute_import, division
from __future__ import print_function

from cctbx import sgtbx

from dials.algorithms.indexing.basis_vector_search import strategies
from dials.algorithms.indexing.basis_vector_search import combinations


def test_combinations(setup_rlp):
    max_cell = 1.3 * max(setup_rlp["crystal_symmetry"].unit_cell().parameters()[:3])
    strategy = strategies.fft1d(max_cell)
    basis_vectors, used = strategy.find_basis_vectors(setup_rlp["rlp"])

    for target_symmetry in (
        setup_rlp["crystal_symmetry"],
        setup_rlp["crystal_symmetry"]
        .primitive_setting()
        .customized_copy(space_group_info=sgtbx.space_group().info()),
        setup_rlp["crystal_symmetry"]
        .primitive_setting()
        .customized_copy(unit_cell=None),
    ):

        crystal_models = combinations.candidate_orientation_matrices(basis_vectors)
        filtered_crystal_models = combinations.filter_known_symmetry(
            crystal_models, target_symmetry=target_symmetry
        )
        filtered_crystal_models = list(filtered_crystal_models)

        assert len(filtered_crystal_models)
        for model in filtered_crystal_models:
            if target_symmetry.unit_cell() is not None:
                assert model.get_unit_cell().minimum_cell().is_similar_to(
                    target_symmetry.minimum_cell().unit_cell(),
                    relative_length_tolerance=0.1,
                    absolute_angle_tolerance=5,
                ) or model.get_unit_cell().is_similar_to(
                    target_symmetry.unit_cell(),
                    relative_length_tolerance=0.1,
                    absolute_angle_tolerance=5,
                )
            else:
                target_sg = (
                    target_symmetry.space_group_info().reference_setting().group()
                )
                from cctbx.sgtbx.lattice_symmetry import metric_subgroups

                subgroups = metric_subgroups(
                    model.get_crystal_symmetry(), max_delta=5, bravais_types_only=False
                )
                if not target_sg.build_derived_patterson_group() in [
                    g["ref_subsym"].space_group() for g in subgroups.result_groups
                ]:
                    assert 0
