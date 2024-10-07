# This code is part of KQCircuits
# Copyright (C) 2022 IQM Finland Oy
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see
# https://www.gnu.org/licenses/gpl-3.0.html.
#
# The software distribution should follow IQM trademark policy for open-source software
# (meetiqm.com/iqm-open-source-trademark-policy). IQM welcomes contributions to the code.
# Please see our contribution agreements for individuals (meetiqm.com/iqm-individual-contributor-license-agreement)
# and organizations (meetiqm.com/iqm-organization-contributor-license-agreement).
from pathlib import Path
from typing import Any, Sequence, Iterable
import gmsh
import numpy as np

try:
    import pya
except ImportError:
    import klayout.db as pya

# prefix to use in case a layer name starts with number or other special character
MESH_LAYER_PREFIX = "elmer_prefix_"

# type alias for dimtag
DimTag = tuple[int, int]


def get_elmer_layers(data):
    """Prefixes dict keys if starting with number. Returns new modified dict"""
    return {apply_elmer_layer_prefix(k): v for k, v in data.items()}


def apply_elmer_layer_prefix(name):
    return name if name[0].isalpha() else MESH_LAYER_PREFIX + name


def produce_mesh(json_data: dict[str, Any], msh_file: Path) -> None:
    """
    Produces mesh and optionally runs the Gmsh GUI

    Args:
        json_data: all the model data produced by `export_elmer_json`
        msh_file: mesh file name
    """

    # Initialize gmsh
    gmsh.initialize()

    # Read geometry from gds file
    layout = pya.Layout()
    layout.read(json_data["gds_file"])
    cell = layout.top_cell()

    # Limiting boundary box (use variable 'box' if it is given. Otherwise, use bounding bo of the geometry.)
    if "box" in json_data:
        bbox = pya.DBox(
            json_data["box"]["p1"]["x"],
            json_data["box"]["p1"]["y"],
            json_data["box"]["p2"]["x"],
            json_data["box"]["p2"]["y"],
        ).to_itype(layout.dbu)
    else:
        bbox = cell.bbox()

    # Create mesh using geometries in gds file
    gmsh.model.add("3D-mesh")
    dim_tags = {}
    layers = json_data["layers"]
    for name, data in layers.items():
        # Get layer region
        if "layer" in data:
            layer_num = data["layer"]
            reg = pya.Region(cell.shapes(layout.layer(layer_num, 0))) & bbox
        else:
            reg = pya.Region(bbox)

        # Convert layer region to polygons
        layer_dim_tags = []
        layer_edge_ids = []
        for simple_poly in reg.each():
            poly = separated_hull_and_holes(simple_poly)
            hull_point_coordinates = [
                (point.x * layout.dbu, point.y * layout.dbu, 0) for point in poly.each_point_hull()
            ]
            hull_plane_surface_id, hull_edge_ids = add_polygon(hull_point_coordinates)
            layer_edge_ids += hull_edge_ids
            hull_dim_tag = (2, hull_plane_surface_id)
            hole_dim_tags = []
            for hole in range(poly.holes()):
                hole_point_coordinates = [
                    (point.x * layout.dbu, point.y * layout.dbu, 0) for point in poly.each_point_hole(hole)
                ]
                hole_plane_surface_id, hole_edge_ids = add_polygon(hole_point_coordinates)
                layer_edge_ids += hole_edge_ids
                hole_dim_tags.append((2, hole_plane_surface_id))
            if hole_dim_tags:
                layer_dim_tags += gmsh.model.occ.cut([hull_dim_tag], hole_dim_tags)[0]
            else:
                layer_dim_tags.append(hull_dim_tag)

        # Move to correct height
        z = data.get("z", 0.0)
        if z != 0.0:
            gmsh.model.occ.translate(layer_dim_tags, 0, 0, z)

        # Thicken sheet
        thickness = data.get("thickness", 0.0)
        if thickness != 0.0:
            extruded = gmsh.model.occ.extrude(layer_dim_tags, 0, 0, thickness)
            layer_dim_tags = [(d, t) for d, t in extruded if d == 3]

            # Create dim_tags instance for edge if edge material is given
            edge_material = data.get("edge_material", None)
            if edge_material is not None:
                edge_extruded = gmsh.model.occ.extrude([(1, i) for i in layer_edge_ids], 0, 0, thickness)
                dim_tags["&" + name] = [(d, t) for d, t in edge_extruded if d == 2]

        # Store layer into dim_tags
        dim_tags[name] = layer_dim_tags

    # Ports
    ports = json_data["ports"]
    for port in ports:
        if "polygon" in port:
            # add port polygon and store its dim_tag
            surface_id, _ = add_polygon(port["polygon"])
            dim_tags[f'port_{port["number"]}'] = [(2, surface_id)]

    # Subtract layers
    for name, data in layers.items():
        subtract = data.get("subtract", [])
        if subtract:
            tool_dim_tags = [t for n in subtract for t in dim_tags[n]]
            dim_tags[name] = gmsh.model.occ.cut(dim_tags[name], tool_dim_tags, removeTool=False)[0]
            gmsh.model.occ.synchronize()

    # Call fragment and get updated dim_tags as new_tags. Then synchronize.
    all_dim_tags = [tag for tags in dim_tags.values() for tag in tags]
    _, dim_tags_map_imp = gmsh.model.occ.fragment(all_dim_tags, [], removeTool=False)
    dim_tags_map = dict(zip(all_dim_tags, dim_tags_map_imp))
    new_tags = {
        name: [new_tag for old_tag in tags for new_tag in dim_tags_map[old_tag]] for name, tags in dim_tags.items()
    }
    gmsh.model.occ.synchronize()

    # Refine mesh
    mesh_size = json_data.get("mesh_size", {})
    mesh_global_max_size = mesh_size.pop("global_max", bbox.perimeter())
    mesh_field_ids = []
    for name, size in mesh_size.items():
        intersection: set[tuple[int, int]] = set()
        split_names = name.split("&")
        if all((name in new_tags for name in split_names)):
            for sname in split_names:
                family = get_recursive_children(new_tags[sname]).union(new_tags[sname])
                intersection = intersection.intersection(family) if intersection else family

            mesh_field_ids += set_mesh_size_field(
                list(intersection - get_recursive_children(intersection)),
                mesh_global_max_size,
                *(size if isinstance(size, list) else [size]),
            )
        else:
            print(f'WARNING: No layers corresponding to mesh_size keys "{split_names}" found')

    # Set meshing options
    workflow = json_data.get("workflow", {})
    n_threads_dict = workflow["sbatch_parameters"] if "sbatch_parameters" in workflow else workflow
    gmsh_n_threads = int(n_threads_dict.get("gmsh_n_threads", 1))
    set_meshing_options(mesh_field_ids, mesh_global_max_size, gmsh_n_threads)

    tls_sheet_layers = {
        name: data
        for name, data in layers.items()
        if any(("_layer" + layer_key in name for layer_key in ["MA", "MS", "SA"])) and data["thickness"] == 0.0
    }

    accepted_thin_materials = ["pec"]
    accepted_thin_materials += list({data["material"] for _, data in tls_sheet_layers.items()})

    materials = list(set(accepted_thin_materials + list(json_data["material_dict"].keys()) + ["vacuum"]))

    filtered_tags_with_material = {}  # store material as the 3rd component of the tuple
    for name, data in layers.items():
        material = data.get("material", None)
        if material in accepted_thin_materials:
            filtered_tags_with_material[name] = [(d, t, material) for d, t in new_tags.get(name, []) if d in [2, 3]]
        elif material in materials:
            filtered_tags_with_material[name] = [(d, t, material) for d, t in new_tags.get(name, []) if d == 3]

        edge_material = data.get("edge_material", None)
        if edge_material in accepted_thin_materials:
            filtered_tags_with_material["&" + name] += [
                (d, t, material) for d, t in new_tags.get("&" + name, []) if d == 2
            ]

    # Group dim tags by material
    material_dim_tags: dict[str, list[DimTag]] = {m: [] for m in materials}
    for _, tags in filtered_tags_with_material.items():
        material = tags[0][2]
        material_dim_tags[material] += [(d, t) for d, t, _ in tags]

    # Sort boundaries of material_dim_tags['pec'] into pec_islands and leave the bodies into material_dim_tags['pec']
    pec_with_boundaries = get_recursive_children(material_dim_tags["pec"]).union(material_dim_tags["pec"])
    pec_islands = [[(d, t)] for d, t in pec_with_boundaries if d == 2]
    material_dim_tags["pec"] = [(d, t) for d, t in pec_with_boundaries if d == 3]

    # Combine touching metal parts to form connected islands
    for i in range(1, len(pec_islands)):
        i_boundary = get_recursive_children(pec_islands[i])
        for j in range(i):
            j_boundary = get_recursive_children(pec_islands[j])
            if i_boundary.intersection(j_boundary):
                pec_islands[i] += pec_islands[j]
                pec_islands[j] = []

    # Assign metal areas as signals and ground
    def _find_island_at(_location, _islands):
        for i, island in enumerate(_islands):
            for dt in island:
                if gmsh.model.isInside(*dt, _location):
                    # isInside doesn't check if _location and entity are in same subspace, so we check it next
                    projection_point = gmsh.model.getClosestPoint(*dt, _location)[0]
                    if all(abs(x - y) < 1e-8 for x, y in zip(_location, projection_point)):
                        return i
        return None

    # signals
    for port in ports:
        if "ground_location" in port:
            # Use 1e-2 safe margin to ensure that signal_location is in the signal polygon:
            signal_location = [x + 1e-2 * (x - y) for x, y in zip(port["signal_location"], port["ground_location"])]
        else:
            signal_location = list(port["signal_location"])
        island_id = _find_island_at(signal_location, pec_islands)
        if island_id is not None:
            material_dim_tags[f'signal_{port["number"]}'] = pec_islands[island_id]
            pec_islands[island_id] = []
    # ground
    counter = 0
    for pec_island in pec_islands:
        if pec_island:
            material_dim_tags[f"ground_{counter}"] = pec_island
            counter += 1

    ports_dts = [dt for port in ports for dt in new_tags.get(f'port_{port["number"]}', [])]
    # set domain boundary as ground for wave equation simulations
    if json_data["tool"] == "wave_equation":
        solid_dts = [(d, t) for dts in material_dim_tags.values() for d, t in dts if d == 3]
        face_dts = [(d, t) for dt in solid_dts for d, t in get_recursive_children([dt]) if d == 2]
        material_dim_tags[f"ground_{counter}"] = [d for d in face_dts if face_dts.count(d) == 1 and d not in ports_dts]

    if json_data["tool"] == "epr_3d":
        # replace 2d pec boundaries with the corresponding 3d bodies
        # also remove 3d pec body containing all of these

        phys_group_tags = {
            k: [(d, t) for d, t, _ in dts] for k, dts in filtered_tags_with_material.items() if dts[0][2] != "pec"
        }

        if material_dim_tags["pec"]:
            for name, dt_list in material_dim_tags.items():
                if name.startswith("signal_") or name.startswith("ground_"):
                    signal_tags_3d = set()
                    for dim, tag in dt_list:
                        parent_pec_tags_3d, _ = gmsh.model.getAdjacencies(dim, tag)
                        parent_pec_tags_3d = [
                            (3, tag) for tag in parent_pec_tags_3d if (3, tag) in material_dim_tags["pec"]
                        ]
                        signal_tags_3d.update(parent_pec_tags_3d)
                    phys_group_tags[name] = list(signal_tags_3d)

    else:
        phys_group_tags = material_dim_tags

    # add physical groups
    for name, dts in phys_group_tags.items():
        no_port_dts = [dt for dt in dts if dt not in ports_dts]
        if no_port_dts:
            gmsh.model.addPhysicalGroup(
                max(dt[0] for dt in no_port_dts), [dt[1] for dt in no_port_dts], name=apply_elmer_layer_prefix(name)
            )

    if json_data["tool"] != "epr_3d":
        # port physical groups
        for port in ports:
            port_name = f'port_{port["number"]}'
            if port_name in new_tags:
                if port["type"] == "EdgePort":
                    port_dts = set(new_tags[port_name])
                    key_dts: dict[str, list[DimTag]] = {"signal": [], "ground": []}
                    for mat, dts in material_dim_tags.items():
                        if mat.startswith("signal"):
                            key_dts["signal"] += [(d, t) for d, t in port_dts.intersection(dts) if d == 2]
                        elif mat.startswith("ground"):
                            key_dts["ground"] += [(d, t) for d, t in port_dts.intersection(dts) if d == 2]
                        elif mat != "pec":
                            key_dts[mat] = [
                                (d, t) for d, t in port_dts.intersection(get_recursive_children(dts)) if d == 2
                            ]
                    for key, dts in key_dts.items():
                        if dts:
                            gmsh.model.addPhysicalGroup(2, [dt[1] for dt in dts], name=f"{port_name}_{key}")
                else:
                    gmsh.model.addPhysicalGroup(2, [dt[1] for dt in new_tags[port_name]], name=port_name)

    # Generate and save mesh
    gmsh.model.mesh.generate(3)
    # Optimize the mesh if the dict exists

    optimize_params = mesh_size.get("optimize", None)

    if optimize_params is not None:
        opt_method = optimize_params.get("method", "Netgen")
        opt_force = optimize_params.get("force", False)
        opt_niter = optimize_params.get("niter", 1)
        opt_dimTags = optimize_params.get("dimTags", [])
        optimizers = [
            "Netgen",
            "HighOrder",
            "HighOrderElastic",
            "HighOrderFastCurving",
            "Laplace2D",
            "Relocate2D",
            "Relocate3D",
            "QuadQuasiStructured",
            "UntangleMeshGeometry",
        ]

        if opt_method in optimizers:
            gmsh.model.mesh.optimize(opt_method, opt_force, opt_niter, opt_dimTags)
        else:
            print(f"WARNING: Wrong optimizer method: {opt_method} chosen at mesh_size dict.")
            print(f"WARNING: Curretly available methods: {optimizers}")

    gmsh.write(str(msh_file))

    # Open mesh viewer
    if workflow.get("run_gmsh_gui", False):
        gmsh.fltk.run()

    gmsh.finalize()


def coord_dist(coord1: Sequence[float], coord2: Sequence[float]) -> float:
    """
    Returns the distance between two points.

    Args:
        coord1: coordinates (x, y, z) of point 1.
        coord2: coordinates (x, y, z) of point 2.

    Returns:
        distance between point 1 and 2
    """
    return float(np.linalg.norm(np.array(coord1) - np.array(coord2)))


def add_polygon(point_coordinates: Sequence[Sequence[float]], mesh_size: float = 0) -> tuple[int, list[int]]:
    """
    Adds the geometry entities in the OpenCASCADE model for generating a polygon and keeps track of all the entities.
    Returns the geometry entity id.

    Args:
        point_coordinates: list of point coordinates that frame the polygon
        mesh_size: mesh element size, default=0

    Returns:
        entity id of the polygon and list of entity ids of edge lines
    """
    points = [gmsh.model.occ.addPoint(*coord, mesh_size) for coord in point_coordinates]
    lines = [gmsh.model.occ.addLine(points[i - 1], points[i]) for i in range(1, len(points))]
    lines.append(gmsh.model.occ.addLine(points[-1], points[0]))
    loops = [gmsh.model.occ.addCurveLoop(lines)]
    return gmsh.model.occ.addPlaneSurface(loops), lines


def separated_hull_and_holes(polygon: pya.Polygon | pya.SimplePolygon) -> pya.Polygon | pya.SimplePolygon:
    """Returns Polygon with holes separated from hull. Takes Polygon or SimplePolygon as the argument."""
    bbox = polygon.bbox().enlarged(10, 10)
    region = pya.Region(bbox) - pya.Region(polygon)
    new_poly = pya.Polygon()
    for p in region.each():
        if p.bbox() == bbox:
            hull_region = pya.Region(bbox) - pya.Region(p)
            new_poly.assign_hull(list(hull_region[0].each_point_hull()), True)
        else:
            new_poly.insert_hole(list(p.each_point_hull()), True)
    return new_poly


def set_mesh_size(
    dim_tags: Sequence[DimTag],
    min_mesh_size: float,
    max_mesh_size: float,
    dist_min: float,
    dist_max: float,
    sampling: float | None = None,
) -> list[int]:
    """
    Set the mesh size such that it is `min_mesh_size` when near the curves of boundaries defined by the entities of
    dim_tags and gradually increasing to `max_mesh_size`.

    .. code-block:: text

      max_mesh_size -                     /------------------
                                         /
                                        /
                                       /
      min_mesh_size -o----------------/
                     |                |    |
                  Point         dist_min  dist_max

    Args:

        dim_tags(list(int, int)): a list of entity dim_tags:
            dimTag (as called in Gmsh) is a tuple of

                * dimension(int): the dimension of the entity (0=point, 1=line, 2=surface, 3=volume)
                * tag(int): the id of the entity

        min_mesh_size: minimum mesh size
        max_mesh_size: maximum mesh size
        dist_min: distance to which the minimum mesh size is used
        dist_max: distance after which the maximum mesh size is used
        sampling: number of sampling points when computing the distance from the curve. The default
                         value is None. In that case the value is determined by 1.5 times the maximum reachable
                         distance in the bounding box of the entity (curve) divided by the minimum mesh size. At
                         the moment there is no obvious way to implement curve_length/min_mesh_size type of
                         algorithm.

    Returns:
        list of the threshold field ids that were defined in this function
    """
    mesh_field_ids = []
    for dim_tag in dim_tags:
        if dim_tag[0] > 2:
            dim_tags += gmsh.model.getBoundary([dim_tag], combined=False, oriented=False, recursive=False)
            continue
        tag_distance_field = gmsh.model.mesh.field.add("Distance")
        key_dict = {0: "PointsList", 1: "CurvesList", 2: "SurfacesList"}
        gmsh.model.mesh.field.setNumbers(tag_distance_field, key_dict[dim_tag[0]], [dim_tag[1]])

        # Sample the object with points
        if sampling is not None:
            # Manual sampling
            gmsh.model.mesh.field.setNumber(tag_distance_field, "Sampling", sampling)
        elif dim_tag[0] > 0:
            # The sampling is determined by 1.5 times the maximum reachable distance in the bounding box of the entity
            # (curve) divided by the minimum mesh size. At the moment there is no obvious way to implement
            # curve_length/min_mesh_size type of algorithm.
            bbox = gmsh.model.occ.getBoundingBox(*dim_tag)
            bbox_diam = coord_dist(bbox[0:3], bbox[3:6])  # diameter of bounding box
            gmsh.model.mesh.field.setNumber(tag_distance_field, "Sampling", np.ceil(1.5 * bbox_diam / min_mesh_size))

        mesh_field_id = gmsh.model.mesh.field.add("Threshold")
        gmsh.model.mesh.field.setNumber(mesh_field_id, "InField", tag_distance_field)
        gmsh.model.mesh.field.setNumber(mesh_field_id, "SizeMin", min_mesh_size)
        gmsh.model.mesh.field.setNumber(mesh_field_id, "SizeMax", max_mesh_size)
        gmsh.model.mesh.field.setNumber(mesh_field_id, "DistMin", dist_min)
        gmsh.model.mesh.field.setNumber(mesh_field_id, "DistMax", dist_max)
        mesh_field_ids.append(mesh_field_id)

    return mesh_field_ids


def set_mesh_size_field(
    dim_tags: Sequence[DimTag],
    global_max: float,
    size: float,
    distance: float | None = None,
    slope: float = 1.0,
) -> list[int]:
    """
    Set the maximal mesh element length in the neighbourhood of the entities given in `dim_tags`. The element size near
    the entities is determined by 'size', 'expansion_dist', and 'expansion_rate'. Further away from the entities the
    element size gradually increases to `global_max`. The maximal mesh elements size as function of distance 'x' from
    the entity is given by min(global_max, size + max(0, x - distance) * slope).

    Args:

        dim_tags: a list of entity dim_tags:
            dimTag (as called in Gmsh) is a tuple of

                * dimension(int): the dimension of the entity (0=point, 1=line, 2=surface, 3=volume)
                * tag(int): the id of the entity

        global_max: global maximal mesh element length
        size: the maximal mesh element length inside at the entity and its expansion
        distance: expansion distance in which the maximal mesh element length is constant (default=size)
        slope: the slope of the increase in the maximal mesh element length outside the entity

    Returns:
        list of the threshold field ids that were defined in this function
    """
    dist = size if distance is None else distance
    return set_mesh_size(dim_tags, size, global_max, dist, dist + (global_max - size) / slope)


def get_recursive_children(dim_tags: Iterable[DimTag]) -> set[DimTag]:
    """Returns children and all recursive grand children of given parent entities

    Args:
        dim_tags: list of dim tags of parent entities

    Returns:
        set of dim tags of all children and recursive grand children
    """
    children: set[DimTag] = set()
    while dim_tags:
        dim_tags = gmsh.model.getBoundary(list(dim_tags), combined=False, oriented=False, recursive=False)
        children = children.union(dim_tags)
    return children


def set_meshing_options(mesh_field_ids: list[int], max_size: float, n_threads: int) -> None:
    """Setup meshing options including mesh size fields and number of parallel threads.

    Args:
        mesh_field_ids: list of the threshold field ids that are given by set_mesh_size function
        max_size: global maximal mesh element length
        n_threads: Number of threads to be used in mesh generation
    """
    background_field_id = gmsh.model.mesh.field.add("Min")
    gmsh.model.mesh.field.setNumbers(background_field_id, "FieldsList", mesh_field_ids)
    gmsh.model.mesh.field.setAsBackgroundMesh(background_field_id)
    gmsh.option.setNumber("Mesh.MeshSizeMax", max_size)
    gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
    gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
    gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
    gmsh.option.setNumber("Mesh.Algorithm", 5)
    gmsh.option.setNumber("Mesh.Algorithm3D", 10)  # HTX
    gmsh.option.setNumber("Mesh.ToleranceInitialDelaunay", 1e-14)
    gmsh.option.setNumber("General.NumThreads", n_threads)
    gmsh.option.setNumber("Mesh.MaxNumThreads1D", n_threads)
    gmsh.option.setNumber("Mesh.MaxNumThreads2D", n_threads)
    gmsh.option.setNumber("Mesh.MaxNumThreads3D", n_threads)
