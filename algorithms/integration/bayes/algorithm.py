#
# algorithm.py
#
#  Copyright (C) 2013 Diamond Light Source
#
#  Author: James Parkhurst
#
#  This code is distributed under the BSD license, a copy of which is
#  included in the root directory of this package.

from __future__ import absolute_import, division, print_function


class IntegrationAlgorithm(object):
    """A class to perform bayesian integration"""

    def __init__(self, **kwargs):
        """
        Initialise algorithm.

        """
        pass

    def __call__(self, reflections, image_volume=None):
        """Process the reflections.

        :param reflections: The reflections to integrate
        :return: The list of integrated reflections

        """
        from dials.array_family import flex

        # Integrate and return the reflections
        if image_volume is None:
            intensity = reflections["shoebox"].bayesian_summation_intensity()
        else:
            raise RuntimeError("Image volume not supported at the moment")
        reflections["intensity.sum.value"] = intensity.observed_value()
        reflections["intensity.sum.variance"] = intensity.observed_variance()
        reflections["background.sum.value"] = intensity.background_value()
        reflections["background.sum.variance"] = intensity.background_variance()
        success = intensity.observed_success()
        reflections.set_flags(success, reflections.flags.integrated_sum)
        return success
