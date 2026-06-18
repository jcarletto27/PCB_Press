import os
import shutil
import zipfile
import tempfile
import re
from pathlib import Path
from typing import List, Optional
import builtins

# --- Monkey-patch for pcb-tools Python 3.11+ compatibility ---
_original_open = builtins.open

def _patched_open(*args, **kwargs):
    args_list = list(args)
    if len(args_list) >= 2 and args_list[1] == 'rU':
        args_list[1] = 'r'
    elif kwargs.get('mode') == 'rU':
        kwargs['mode'] = 'r'
    return _original_open(*args_list, **kwargs)

builtins.open = _patched_open
# --------------------------------------------------------------

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import gerber
import trimesh
import shapely
from shapely.geometry import Point, LineString, Polygon, MultiPolygon
from shapely import ops, affinity

# Initialize the FastAPI app
app = FastAPI(title="PCB Press API")

# Setup directories for static files (frontend) and generated models
BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
MODELS_DIR = STATIC_DIR / "models"
UPLOAD_DIR = BASE_DIR / "uploads"

STATIC_DIR.mkdir(exist_ok=True)
MODELS_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

# Mount the static directory so the frontend can access the STLs
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

class GenerateConfig(BaseModel):
    upload_folder_id: str
    outline_layer: str
    trace_layer: str
    via_layer: str
    base_thickness: float = 2.0
    trace_thickness: float = 1.0
    mirror: bool = False
    margin_offset: float = 0.2

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        with open(index_path, "r") as f:
            return f.read()
    return "<h1>PCB Press</h1><p>Frontend not found. Please create static/index.html.</p>"

@app.post("/api/upload")
async def upload_gerber_zip(file: UploadFile = File(...)):
    if not file.filename.endswith('.zip'):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")

    temp_dir = tempfile.mkdtemp(dir=UPLOAD_DIR)
    zip_path = Path(temp_dir) / file.filename

    with open(zip_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    extract_dir = Path(temp_dir) / "extracted"
    extract_dir.mkdir()
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid or corrupt ZIP file")

    available_layers = []
    for root, dirs, files in os.walk(extract_dir):
        for f in files:
            if not f.lower().endswith(('.json', '.pdf', '.md', '.txt', '.png', '.jpg')):
                available_layers.append(f)

    return {
        "status": "success",
        "upload_folder_id": os.path.basename(temp_dir),
        "layers": available_layers
    }

def auto_scale_geom(geom, reference_geom):
    """Auto-detects and fixes common unit mismatches."""
    if geom.is_empty or reference_geom.is_empty:
        return geom
        
    g_bounds = geom.bounds
    r_bounds = reference_geom.bounds
    
    g_w = g_bounds[2] - g_bounds[0]
    r_w = r_bounds[2] - r_bounds[0]
    
    if g_w == 0 or r_w == 0:
        return geom
        
    ratio = r_w / g_w
    scales = [25.4, 1/25.4, 10.0, 0.1, 100.0, 0.01, 1000.0, 0.001, 254.0, 1/254.0]
    best_scale = 1.0
    
    for s in scales:
        if abs(s - ratio) / s < 0.3:
            best_scale = s
            break
            
    if best_scale != 1.0:
        return affinity.scale(geom, xfact=best_scale, yfact=best_scale, origin=(0,0))
    return geom

def flatten_to_polygons(geom):
    """Strictly extracts only Polygons, bypassing Shapely 'create_collection' errors."""
    if geom.is_empty:
        return []
    if geom.geom_type == 'Polygon':
        return [geom]
    elif geom.geom_type == 'MultiPolygon':
        return list(geom.geoms)
    elif geom.geom_type == 'GeometryCollection':
        res = []
        for g in geom.geoms:
            res.extend(flatten_to_polygons(g))
        return res
    return []

def parse_drl_to_shapely(filepath):
    """Directly converts an Excellon drill file to Shapely polygons."""
    tools = {}
    current_tool = None
    polys = []
    last_x, last_y = 0.0, 0.0
    
    with open(filepath, 'r') as f:
        lines = f.readlines()
        
    for line in lines:
        line = line.strip()
        if not line: continue
        
        tool_def = re.match(r'^T0*(\d+)C([\d.]+)', line)
        if tool_def:
            tools[tool_def.group(1)] = float(tool_def.group(2))
            continue
            
        tool_sel = re.match(r'^T0*(\d+)$', line)
        if tool_sel:
            current_tool = tool_sel.group(1)
            continue
            
        x_match = re.search(r'X([-\d.]+)', line)
        y_match = re.search(r'Y([-\d.]+)', line)
        
        if x_match or y_match:
            if x_match: 
                val = x_match.group(1)
                last_x = float(val) if '.' in val else float(val) / 10000.0
            if y_match: 
                val = y_match.group(1)
                last_y = float(val) if '.' in val else float(val) / 10000.0
            
            dia = tools.get(current_tool, 0.8)
            polys.append(Point(last_x, last_y).buffer(dia / 2.0, resolution=8))
            
    valid_polys = []
    for p in polys:
        if p.is_valid and not p.is_empty:
            valid_polys.extend(flatten_to_polygons(p))
            
    if not valid_polys:
        return Polygon()
    return ops.unary_union(valid_polys)

def extract_shapely_polys(doc, is_outline=False):
    """Converts standard Gerber files into a unified Shapely Polygon geometry."""
    polys = []
    
    for p in getattr(doc, 'primitives', []):
        p_type = type(p).__name__
        
        if p_type == 'Line':
            dia = p.aperture.diameter if hasattr(p, 'aperture') and hasattr(p.aperture, 'diameter') else 0.0
            if dia > 0 and p.start != p.end:
                polys.append(LineString([p.start, p.end]).buffer(dia / 2.0, resolution=8))
            elif is_outline and p.start != p.end:
                polys.append(LineString([p.start, p.end]).buffer(0.01, resolution=4))
                
        elif p_type == 'Circle':
            dia = p.diameter if hasattr(p, 'diameter') else 0.0
            polys.append(Point(p.position).buffer(dia / 2.0, resolution=8))
            
        elif p_type == 'Flash':
            if hasattr(p, 'aperture'):
                ap_type = type(p.aperture).__name__
                if ap_type == 'CircleAperture':
                    polys.append(Point(p.position).buffer(p.aperture.diameter / 2.0, resolution=8))
                elif ap_type == 'RectangleAperture':
                    w, h = p.aperture.width, p.aperture.height
                    x, y = p.position
                    polys.append(Polygon([
                        (x - w/2, y - h/2), (x + w/2, y - h/2),
                        (x + w/2, y + h/2), (x - w/2, y + h/2)
                    ]))
                    
        elif p_type == 'Region':
            pts = []
            for rp in p.primitives:
                if type(rp).__name__ == 'Line':
                    if not pts: pts.append(rp.start)
                    pts.append(rp.end)
            if len(pts) >= 3:
                try:
                    polys.append(Polygon(pts))
                except Exception:
                    pass
                
    valid_polys = []
    for p in polys:
        try:
            if not p.is_valid:
                p = shapely.make_valid(p)
            valid_polys.extend(flatten_to_polygons(p))
        except Exception:
            pass
            
    if not valid_polys:
        return Polygon()
        
    merged = ops.unary_union(valid_polys)
    
    if is_outline:
        poly_list = flatten_to_polygons(merged)
        if not poly_list:
            return Polygon()
        largest = max(poly_list, key=lambda g: g.area)
        return Polygon(largest.exterior)
        
    return merged

def extrude_geom(geom, height):
    """Safely converts 2D shapely geometry into watertight 3D trimesh volumes."""
    meshes = []
    
    # Aggressively clean the 2D geometry to remove micro-overlaps and zero-area artifacts
    try:
        clean_geom = geom.buffer(0).simplify(0.001)
    except Exception:
        clean_geom = geom
        
    poly_list = flatten_to_polygons(clean_geom)
    
    for poly in poly_list:
        if not poly.is_empty and poly.area > 0.001:
            try:
                mesh = trimesh.creation.extrude_polygon(poly, height=height)
                mesh.fix_normals()
                
                # Manifold engine strictly requires perfectly watertight volumes. 
                # We drop any broken microscopic fragments here.
                if mesh.is_volume:
                    meshes.append(mesh)
            except Exception:
                pass
                
    if not meshes:
        return trimesh.Trimesh()
    
    return trimesh.util.concatenate(meshes)

@app.post("/api/generate")
async def generate_stls(config: GenerateConfig):
    extract_dir = UPLOAD_DIR / config.upload_folder_id / "extracted"
    if not extract_dir.exists():
        raise HTTPException(status_code=404, detail="Upload folder not found. Please re-upload.")

    try:
        # 1. Parse ONLY the standard Gerbers (Outline and Traces) with pcb-tools
        outline_doc = gerber.read(str(extract_dir / config.outline_layer))
        trace_doc = gerber.read(str(extract_dir / config.trace_layer))

        for d in [outline_doc, trace_doc]:
            d.to_metric()

        # 2. Extract 2D Shapely Geometry for Gerbers
        outline_geom = extract_shapely_polys(outline_doc, is_outline=True)
        trace_geom = extract_shapely_polys(trace_doc)
        trace_geom = auto_scale_geom(trace_geom, outline_geom)

        # 3. Parse SHAPELY GEOMETRY FOR VIAS
        via_geom = parse_drl_to_shapely(str(extract_dir / config.via_layer))
        via_geom = auto_scale_geom(via_geom, outline_geom)

        # 3.5 Apply mirroring directly to 2D geometry before extrusion
        if config.mirror:
            outline_geom = affinity.scale(outline_geom, xfact=-1.0, origin=(0, 0)).buffer(0)
            trace_geom = affinity.scale(trace_geom, xfact=-1.0, origin=(0, 0)).buffer(0)
            via_geom = affinity.scale(via_geom, xfact=-1.0, origin=(0, 0)).buffer(0)

        # 4. Extrude Base Board
        base_mesh = extrude_geom(outline_geom, config.base_thickness)
        if base_mesh.is_empty or not base_mesh.is_volume:
            raise HTTPException(status_code=400, detail="Could not form a solid watertight board from the Outline layer. Ensure the outline is perfectly closed.")

        # 5. Extrude Traces
        trace_mesh = extrude_geom(trace_geom, config.trace_thickness + 0.1)
        if not trace_mesh.is_empty:
            trace_mesh.apply_translation([0, 0, config.base_thickness - config.trace_thickness])

        # 6. Extrude Vias / Drills
        via_mesh = extrude_geom(via_geom, config.base_thickness + 10.0)
        if not via_mesh.is_empty:
            via_mesh.apply_translation([0, 0, -5.0])

        # 7. Boolean Subtractions for Base Board
        final_board = base_mesh
        if not trace_mesh.is_empty and trace_mesh.is_volume:
            final_board = trimesh.boolean.difference([final_board, trace_mesh], engine='manifold')
        if not via_mesh.is_empty and via_mesh.is_volume:
            final_board = trimesh.boolean.difference([final_board, via_mesh], engine='manifold')


        # --- COMPANION MOLD GENERATION ---
        bounds = base_mesh.bounds
        mold_width = bounds[1][0] - bounds[0][0] + 10
        mold_height = bounds[1][1] - bounds[0][1] + 10
        center_x = (bounds[1][0] + bounds[0][0]) / 2.0
        center_y = (bounds[1][1] + bounds[0][1]) / 2.0

        mold_base = trimesh.creation.box(extents=[mold_width, mold_height, 2.0])
        mold_base.apply_translation([center_x, center_y, 1.0])

        buffered_trace_geom = trace_geom.buffer(-config.margin_offset)
        mold_traces = extrude_geom(buffered_trace_geom, config.trace_thickness)
        
        if not mold_traces.is_empty and mold_traces.is_volume:
            mold_traces.apply_translation([0, 0, 2.0])
            final_mold = trimesh.boolean.union([mold_base, mold_traces], engine='manifold')
        else:
            final_mold = mold_base

        if not via_mesh.is_empty and via_mesh.is_volume:
            final_mold = trimesh.boolean.difference([final_mold, via_mesh], engine='manifold')

        # --- CENTER MODELS AT ORIGIN ---
        translation_matrix = trimesh.transformations.translation_matrix([-center_x, -center_y, 0])
        final_board.apply_transform(translation_matrix)
        final_mold.apply_transform(translation_matrix)

        # --- EXPORT STLS ---
        main_filename = f"{config.upload_folder_id}_main.stl"
        mold_filename = f"{config.upload_folder_id}_mold.stl"
        
        final_board.export(MODELS_DIR / main_filename)
        final_mold.export(MODELS_DIR / mold_filename)

        return {
            "status": "success",
            "message": "STLs generated successfully.",
            "main_model_url": f"/static/models/{main_filename}",
            "mold_model_url": f"/static/models/{mold_filename}"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Geometry Generation Error: {str(e)}")