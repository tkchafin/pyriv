import numpy as np
import networkx as nx
from networkx import NetworkXNoPath
import pandas as pd
import geopandas as gpd
from shapely.ops import polygonize
from shapely.geometry import MultiPolygon, LineString, Point
from units import length_in_display_units
from river_graph import point_to_tuple
from multiprocessing import Pool
from functools import partial
from itertools import chain, combinations
from ast import literal_eval
import os
import json

#%% For testing
test_data_dir = '/home/jkibele/sasap-size-declines/RiverDistance/data/test_data/'
#test_data_dir = '/Users/jkibele/Documents/SASAP/sasap-size-declines/RiverDistance/data/test_data/'
fullpath = lambda fn: os.path.join(test_data_dir, fn)
coastfn = fullpath('CoastLine.shp')
rivfn = fullpath('Rivers.shp')
testlinefn = fullpath('test_lines.shp')
rivmouthsfn = fullpath('RiverMouths.shp')
uprivpoints = fullpath('UpriverTestPoints.shp')
islandgraphmlfn = fullpath('island.graphml')
islandgpickle = fullpath('island.gpickle')


#%% Make land polygon from coastline. Coasline is actually many linestrings.
def polygonize_coastline(coast):
    """Turn the coastline into a multipolygon.
    
    Take a geodataframe or a path to a shapefile and return a land multipolygon.
    """
    if coast.__class__.__name__ != 'GeoDataFrame':
        coast = gpd.read_file(coast)
    plist = [p for p in polygonize(coast.geometry)]
    return MultiPolygon(plist)

def coords_from_coastline(coast):
    """Extract coordinates from a coastline shapefile or geodataframe.
    
    This serves pretty much the same purpose as `extract_poly_exterior_coords`.
    We may not need to keep both versions.
    """
    if coast.__class__.__name__ != 'GeoDataFrame':
        coast = gpd.read_file(coast)
    
#    print "geom type: {}".format(coast.geometry.iloc[0].type)
    if coast.geometry.iloc[0].type.lower().find('poly') > -1:
        fltlst = extract_poly_exterior_coords(coast.unary_union)
    else: # assume it's a line feature
        clst = coast.geometry.apply(lambda l: l.coords).tolist()
        fltlst = [item for sublist in clst for item in sublist]
    return fltlst

def extract_poly_exterior_coords(geom):
    """Extract the exterior coordinates from a polygon or multipolygon.
    
    Returns
    -------
    list of tuples
        All the exterior coordinates of geom.
    
    Notes
    -----
    This code was adapted from an answer on stackexchange:
    https://gis.stackexchange.com/questions/119453/count-the-number-of-points-in-a-multipolygon-in-shapely
    """
    if geom.type == 'Polygon':
        exterior_coords = geom.exterior.coords[:]
    elif geom.type == 'MultiPolygon':
        exterior_coords = []
        for part in geom:
            epc = extract_poly_exterior_coords(part)  # Recursive call
            exterior_coords += epc[:]
    else:
        raise ValueError('Unhandled geometry type: ' + repr(geom.type))
    return exterior_coords

def graph_from_coast(coast):
    """Build a networkx graph for coastal swimmable distance calculations.
    
    """
    if coast.__class__.__name__ != 'GeoDataFrame':
        coast = gpd.read_file(coast)

def radius_filter(node, node_list, radius=5000):
    node = Point(node)
    pnts = [Point(n) for n in node_list]
    keep = [tuple(np.array(p)) for p in pnts if node.distance(p) <= radius]
    return keep

def ocean_edges_for_node(node, land_obj, node_list, radius=None):
    """
    multiprocessing can't work with functions that are not a top level, so I'm
    moving this out here to see if I can make it work.
    """
    if radius:
        node_list = radius_filter(node, node_list, radius=radius)

    ocean_edges = tuple()
    for n in node_list:
        if node <> n:
            line = LineString((node,n))
            if not land_obj.line_crosses(line):
                ocean_edges += ((node, n, {'distance': length_in_display_units(line.length)}),)
                # print '.',
    return ocean_edges

def closest_node(graph, pos):
    """
    Return the closest node to the given (x,y) position.

    Parameters
    ----------
      pos : tuple or list or shapely.geometry.point.Point
        Coordinates as (x, y), i.e., (lon, lat) not (lat, lon). If shapely
        point, will be converted to tuple.

    Returns
    -------
      tuple
        (x, y) coordinates of closest node.
    """

    nodes = np.array(graph.nodes())
    node_pos = np.argmin(np.sum((nodes - pos)**2, axis=1))
    return tuple(nodes[node_pos])

def get_path(graph, n0, n1):
    """
    If n0 and n1 are adjacent connected nodes in the graph, this function
    return an array of point coordinates along the river linking
    these two nodes.
    """
    try:
        linepath = np.array(json.loads(graph[n0][n1]['Json'])['coordinates'])
    except KeyError:
        linepath = np.array((n0, n1))
    return linepath


def get_full_path(graph, short_path):
    """
    This will take a path consisting of just nodes, and return the
    full path that contains all the vertices between nodes as well.
    """
    pnts = []
    for i, pnt in enumerate(short_path[:-1]):
        p = get_path(graph, pnt, short_path[i+1])
        pnts.append(p)
    return np.vstack(pnts)

def shortest_full_path(graph, node0, node1, weights='distance'):
    """
    Find the shortest full path between 2 positions. This will find
    the nodes closest to the given positions, the nodes between those
    nodes for the shortest path, and fill in all the available
    vertices in the linestring.

    Parameters
    ----------
      node0 : tuple or array-like
        x,y (lon,lat) coordinates for the start point
      node1 : tuple or array-like
        x,y (lon,lat) coordinates for the end point
      graph : NetworkX Graph or DiGraph object
        The Graph

    Returns
    -------
      shapely.geometry.linestring.LineString
        A geometry representing the shortest full path.
    """
    # find the nodes that define the shortest path between nodes
    pth = nx.shortest_path(graph, node0, node1, weight=weights)
    # fill in the missing vertices
    pth = get_full_path(graph, pth)
    # convert to linestring and return
    return LineString(pth)

def coastal_fish_distance(graph, pos0, pos1, weights='distance', raise_fail=True):
    """Find the shortest fish-distance (aka minimum aquatic distance) line from 
    one position to another.
    
    Parameters
    ----------
      graph : nx.Graph
        Graph representation of the combined river and coastal network.
      pos0 : tuple or list or shapely.geometry.point.Point
        Coordinates as (x, y), i.e., (lon, lat) not (lat, lon). If shapely
        point, will be converted to tuple.
      pos1 : tuple or list or shapely.geometry.point.Point
        Coordinates as (x, y), i.e., (lon, lat) not (lat, lon). If shapely
        point, will be converted to tuple.
    
    """
    node0 = closest_node(graph, pos0)
    node1 = closest_node(graph, pos1)
    if node0 == node1:
        pth = None
    else:
        try:
            pth = shortest_full_path(graph, node0, node1, weights=weights)
        except NetworkXNoPath:
            pth = None
            if raise_fail:
                raise NetworkXNoPath("No network path between these nodes.")
    # convert to linestring and return
    return LineString(pth)

def mad_df(graph, locations, label_column, weights='distance', raise_fail=True):
    """Create a minimum aquatic distance (MAD) geodataframe for all points 
    in `locations`.
    
    Parameters
    ----------
      graph : nx.Graph
        Graph representation of the combined river and coastal network.
      locations : geopandas.GeoDataFrame or a string file path
        A point GeoDataFrame (or file path that can be opened by 
        `geopandas.read_file`) representing the locations that you want
        a MAD matrix for.
      label_column : string
        The name of the column in `locations` that contains labels you
        want to use in the output.
      
    Returns
    -------
      geopandas.geodataframe
        A geodataframe containing MAD paths between each point in 
        `locations`. In addtion to the path geometries, it will also 
        have the following columns:
          from: the `label_column` value for the starting point
          to: the `label_column` value for the end point
          failure_flag: 0 if a path was found between the points
            and 1 if no path was found
          dist_km: The minimum aquatic distance (in kilometers)
            between the `from` and `to` locations.
            
    Notes
    -----
      The locations and the graph are assumed to be in a projection with
      meters as the unit of distance.
    """
    if locations.__class__.__name__ == "GeoDataFrame":
        locs = locations
    else:
        locs = gpd.read_file(locations)
    lcol = label_column
        
    combs = combinations(locs.index, 2)
    path_rows = []
    for indA, indB in combs:
        A = locations.loc[indA]
        B = locations.loc[indB]
        try:
            pth = coastal_fish_distance(graph, A.geometry, B.geometry, 
                                        weights=weights, raise_fail=True)
            fail_flag = 0
        except NetworkXNoPath:
            pth = LineString()
            fail_flag = 1
            if raise_fail:
                raise NetworkXNoPath("No network path between {} and {}.".format(A[lcol], B[lcol]))
        path_rows.append([A[lcol], B[lcol], fail_flag, pth])
        
    pathdf = gpd.GeoDataFrame(path_rows, columns=['from', 'to', 'failure_flag', 'geometry'])
    pathdf.crs = locations.crs
    pathdf['dist_km'] = pathdf.length * 1e-3
    return pathdf

def mad_matrix(mad_dataframe):
    """Turn a mad_dataframe (output of `mad_df`) into a matrix format.
    
    """
    mdf = mad_dataframe
    labels = mdf['from'].append(mdf['to']).unique()
    labels.sort()
    dist_rows = []
    for sc_row in labels:
        drow = []
        for sc_col in labels:
            if sc_col == sc_row:
                drow.append(0.0)
            else:
                dval_ind = ((mdf['from']==sc_row) & (mdf['to']==sc_col))
                if not dval_ind.any():
                    dval_ind = ((mdf['from']==sc_col) & (mdf['to']==sc_row))
                    if not dval_ind.any():
                        print "There's a problem with {} and {}.".format(sc_row, sc_col)
                drow.append(mdf.loc[dval_ind, 'dist_km'].item())
        dist_rows.append(drow)

    return pd.DataFrame(dist_rows, index=labels, columns=labels)
    

def cfd_to_pos_list(graph, pos0, pos_list, weights='distance'):
    """Find coastal fish distance from pos0 to a list of positions.
    """
    line_list = list()
    for pos in pos_list:
        pth = coastal_fish_distance(graph, pos0, pos, weights=weights)
        line_list.append(pth)
    return line_list


#%% Land Obj
class Land(object):
    def __init__(self, coast, graph=None, shrink=1.0):
        
        if coast.__class__.__name__ != 'GeoDataFrame':
            coast = gpd.read_file(coast)
            
        self.gdf = coast
        self.land_poly = polygonize_coastline(self.gdf)
        self.land_shrunk = self.land_poly.buffer(-1*shrink)
        self.coords = coords_from_coastline(self.gdf)
        if type(graph).__name__ in ('Graph','MultiGraph','DiGraph','MultiDiGraph'):
            self.cached_graph = graph
        elif type(graph).__name__ == 'str':
            self.cached_graph = nx.read_gpickle(graph)
        else:
            self.cached_graph = None
        
    def graph(self, dump_cached=False, n_jobs=6, verbose=False):
        if dump_cached or not self.cached_graph:
            G = self.fresh_graph()
            G = self._add_ocean_edges_complete(G, verbose=verbose, n_jobs=n_jobs)
            self.cached_graph = G
        return self.cached_graph
    
    def fresh_graph(self):
        G = nx.Graph()
        G.add_nodes_from(self.coords)
        return G
    
    def line_crosses(self, line):
        """Determine whether or not the given line crosses shrunken land.
        
        """
        return line.intersects(self.land_shrunk)
    
    def add_ocean_edges(self, nodes, n_jobs=6, radius=None):
        oe_node = partial(ocean_edges_for_node, land_obj=self, node_list=nodes, radius=radius)
        pool = Pool(processes=n_jobs)
        ocean_edges = pool.map(oe_node, self.cached_graph.nodes_iter())
        # map returns a list of tuples (one for all the edges of each node). We
        # need that flattened into a single iterable of all the edges.
        ocean_edges = chain.from_iterable(ocean_edges)
        self.cached_graph.add_edges_from(ocean_edges)
        return self.cached_graph
    
    def _add_ocean_edges_complete(self, graph, n_jobs=6, radius=None, verbose=False):
        if verbose:
            import time
            t0 = time.time()
            print "Starting at %s to add edges for %i nodes." % (time.asctime(time.localtime(t0)), graph.number_of_nodes() )
            edge_possibilities = graph.number_of_nodes() * (graph.number_of_nodes() -1)
            print "We'll have to look at somewhere around %i edge possibilities." % ( edge_possibilities )
            print "Node: ",
        oe_node = partial(ocean_edges_for_node, land_obj=self, node_list=graph.nodes(), radius=radius)
        pool = Pool(processes=n_jobs)
        ocean_edges = pool.map(oe_node, graph.nodes_iter())
        # map returns a list of tuples (one for all the edges of each node). We
        # need that flattened into a single iterable of all the edges.
        ocean_edges = chain.from_iterable(ocean_edges)
        graph.add_edges_from(ocean_edges)
        if verbose:
            print "It took %i minutes to load %i edges." % ((time.time() - t0)/60, graph.number_of_edges() )
        return graph