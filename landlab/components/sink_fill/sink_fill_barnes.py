#!/usr/env/python

"""
fill_sinks_barnes.py

Fill sinks in a landscape to the brim, following the Barnes et al. (2014) algos.
"""

from __future__ import print_function

import warnings

from landlab import FieldError, Component, BAD_INDEX_VALUE
from landlab import RasterModelGrid, VoronoiDelaunayGrid  # for type tests
from landlab.components import LakeMapperBarnes
from landlab.utils.return_array import return_array_at_node
from landlab.core.messages import warning_message
from landlab.components.lake_fill.lake_fill_barnes import StablePriorityQueue

from landlab import FIXED_VALUE_BOUNDARY, FIXED_GRADIENT_BOUNDARY
from landlab import CLOSED_BOUNDARY
from collections import deque
import six
import numpy as np
import heapq
import itertools

LOCAL_BAD_INDEX_VALUE = BAD_INDEX_VALUE


class SinkFillerBarnes(LakeMapperBarnes):
    """
    Uses the Barnes et al (2014) algorithms to replace pits with flats, or
    optionally to very shallow gradient surfaces to allow continued draining.

    This component is NOT intended for use iteratively as a model runs;
    rather, it is to fill in an initial topography. If you want to repeatedly
    fill pits as a landscape develops, you are after the SinkFillerBarnes
    component.

    The locations and depths etc. of the fills will be tracked, and properties
    are provided to access this information.

    Parameters
    ----------
    grid : ModelGrid
        A grid.
    surface : field name at node or array of length node
        The surface to fill.
    method : {'steepest', 'd8'}
        Whether or not to recognise diagonals as valid flow paths, if a raster.
        Otherwise, no effect.
    fill_flat : bool
        If True, pits will be filled to perfectly horizontal. If False, the new
        surface will be slightly inclined to give steepest descent flow paths
        to the outlet.
    ignore_overfill : bool
        If True, suppresses the Error that would normally be raised during
        creation of a gentle incline on a fill surface (i.e., if not
        fill_flat). Typically this would happen on a synthetic DEM where more
        than one outlet is possible at the same elevation. If True, the
        was_there_overfill property can still be used to see if this has
        occurred.
    """
    def __init__(self, grid, surface='topographic__elevation',
                 method='d8', fill_flat=False,
                 ignore_overfill=False):
        # Most of the functionality of this component is directly inherited
        # from SinkFillerBarnes, so
        super(SinkFillerBarnes, self).__init__(
            grid, surface=surface, method=method, fill_flat=fill_flat,
            fill_surface=surface, redirect_flow_steepest_descent=False,
            reaccumulate_flow=False, ignore_overfill=ignore_overfill,
            track_lakes=True)
        # note we will always track the fills, since we're only doing this
        # once... Likewise, no need for flow routing; this is not going to
        # get used dynamically.
        self._supplied_surface = return_array_at_node(grid, surface).copy()

    def run_one_step(self):
        """
        Fills the surface to remove all pits.

        Examples
        --------
        >>> import numpy as np
        >>> from landlab import RasterModelGrid, CLOSED_BOUNDARY
        >>> from landlab.components import SinkFillerBarnes, FlowRouter
        >>> mg = RasterModelGrid((5, 6), 1.)
        >>> for edge in ('left', 'top', 'bottom'):
        ...     mg.status_at_node[mg.nodes_at_edge(edge)] = CLOSED_BOUNDARY
        >>> z = mg.add_zeros('node', 'topographic__elevation', dtype=float)
        >>> z.reshape(mg.shape)[2, 1:-1] = [2., 1., 0.5, 1.5]
        >>> z.reshape(mg.shape)[1, 1:-1] = [2.1, 1.1, 0.6, 1.6]
        >>> z.reshape(mg.shape)[3, 1:-1] = [2.2, 1.2, 0.7, 1.7]
        >>> z_init = z.copy()
        >>> sfb = SinkFillerBarnes(mg, method='steepest')  #, surface=z

        TODO: once return_array_at_node is fixed, this example should also
        take surface... GIVE IT surface=z  !!

        >>> sfb.run_one_step()
        >>> z_out = np.array([ 0. ,  0. ,  0. ,  0. ,  0. ,  0. ,
        ...                    0. ,  2.1,  1.5,  1.5,  1.6,  0. ,
        ...                    0. ,  2. ,  1.5,  1.5,  1.5,  0. ,
        ...                    0. ,  2.2,  1.5,  1.5,  1.7,  0. ,
        ...                    0. ,  0. ,  0. ,  0. ,  0. ,  0. ])
        >>> # np.allclose(z, z_out)  ->  True once fixed
        >>> np.all(np.equal(z, z_out))  # those 1.5's are actually a bit > 1.5
        False
        >>> fill_map = np.array([-1, -1, -1, -1, -1, -1,
        ...                      -1, -1, 16, 16, -1, -1,
        ...                      -1, -1, 16, 16, -1, -1,
        ...                      -1, -1, 16, 16, -1, -1,
        ...                      -1, -1, -1, -1, -1, -1])
        >>> np.all(sfb.fill_map == fill_map)
        True
        >>> np.all(sfb.fill_at_node == (sfb.fill_map > -1))  # bool equivalent
        True
        >>> sfb.was_there_overfill  # everything fine with slope adding
        False

        >>> fr = FlowRouter(mg, method='D4')  # routing will work fine now
        >>> fr.run_one_step()
        >>> np.all(mg.at_node['flow__sink_flag'][mg.core_nodes] == 0)
        True
        >>> drainage_area = np.array([  0.,   0.,   0.,   0.,   0.,   0.,
        ...                             0.,   1.,   2.,   3.,   1.,   1.,
        ...                             0.,   1.,   4.,   9.,  10.,  10.,
        ...                             0.,   1.,   2.,   1.,   1.,   1.,
        ...                             0.,   0.,   0.,   0.,   0.,   0.])
        >>> np.allclose(mg.at_node['drainage_area'], drainage_area)
        True

        Test two pits and a flat fill:

        >>> z[:] = mg.node_x.max() - mg.node_x
        >>> z[[10, 23]] = 1.1  # raise "guard" exit nodes
        >>> z[7] = 2.  # is a lake on its own
        >>> z[9] = 0.5
        >>> z[15] = 0.3
        >>> z[14] = 0.6  # [9, 14, 15] is a lake
        >>> z[22] = 0.9  # a non-contiguous lake node also draining to 16
        >>> z_init = z.copy()
        >>> sfb = SinkFillerBarnes(mg, method='steepest', fill_flat=True)
        >>> sfb.run_one_step()
        >>> sfb.fill_dict  == {8: deque([7]), 16: deque([15, 9, 14, 22])}
        True
        >>> sfb.number_of_fills
        2
        >>> sfb.fill_outlets == [16, 8]
        True
        >>> np.allclose(sfb.fill_areas, np.array([4., 1.]))  # same order
        True

        Unlike the LakeMapperBarnes equivalents, fill_depths and fill_volumes
        are always available through this component:

        >>> fill_depths = np.array([ 0. ,  0. ,  0. ,  0. ,  0. ,  0. ,
        ...                          0. ,  1. ,  0. ,  0.5,  0. ,  0. ,
        ...                          0. ,  0. ,  0.4,  0.7,  0. ,  0. ,
        ...                          0. ,  0. ,  0. ,  0. ,  0.1,  0. ,
        ...                          0. ,  0. ,  0. ,  0. ,  0. ,  0. ])
        >>> np.allclose(sfb.fill_depths, fill_depths)
        True
        >>> np.allclose(sfb.fill_volumes, np.array([1.7, 1.]))
        True

        Note that with a flat fill, we can't drain the surface. The surface
        is completely flat, so each and every core node within the fill
        becomes a sink.

        >>> fr.run_one_step()
        >>> where_is_filled = np.where(sfb.fill_map > -1, 1, 0)
        >>> np.all(
        ...     mg.at_node['flow__sink_flag'][mg.core_nodes] ==
        ...     where_is_filled[mg.core_nodes])
        True

        (Note that the fill_map does not think that the perimeter nodes are
        sinks, since they haven't changed elevation. In contrast, the
        FlowRouter *does* think they are, because these nodes are where flow
        terminates.)
        """
        super(SinkFillerBarnes, self).run_one_step()

    @property
    def fill_dict(self):
        """
        Return a dictionary where the keys are the outlet nodes of each filled
        area, and the values are deques of nodes within each. Items are not
        returned in ID order.
        """
        return super(SinkFillerBarnes, self).lake_dict

    @property
    def fill_outlets(self):
        """
        Returns the outlet for each filled area, not necessarily in ID order.
        """
        return super(SinkFillerBarnes, self).lake_outlets

    @property
    def number_of_fills(self):
        """
        Return the number of individual filled areas.
        """
        return super(SinkFillerBarnes, self).number_of_lakes

    @property
    def fill_map(self):
        """
        Return an array of ints, where each filled node is labelled
        with its outlet node ID.
        Nodes not in a filled area are labelled with LOCAL_BAD_INDEX_VALUE
        (default -1).
        """
        return super(SinkFillerBarnes, self).lake_map

    @property
    def fill_at_node(self):
        """
        Return a boolean array, True if the node is filled, False otherwise.
        """
        return super(SinkFillerBarnes, self).lake_at_node

    @property
    def fill_depths(self):
        """Return the change in surface elevation at each node this step.
        """
        return self._surface - self._supplied_surface

    @property
    def fill_areas(self):
        """
        A nlakes-long array of the area of each fill. The order is the same as
        that of the keys in fill_dict, and of fill_outlets.
        """
        return super(SinkFillerBarnes, self).lake_areas

    @property
    def fill_volumes(self):
        """
        A nlakes-long array of the volume of each fill. The order is the same
        as that of the keys in fill_dict, and of fill_outlets.
        """
        fill_vols = np.empty(self.number_of_fills, dtype=float)
        col_vols = self.grid.cell_area_at_node * self.fill_depths
        for (i, (outlet, fillnodes)) in enumerate(
             self.fill_dict.iteritems()):
            fill_vols[i] = col_vols[fillnodes].sum()
        return fill_vols