# PCB Press

*Lovingly based on the free SVG to PCB model called *[*PCB Forge by castpixel*](https://castpixel.itch.io/pcb-forge)*.*

## Overview

PCB Press allows hardware developers to bypass the mess of chemical etching and the fragility of CNC milling. By uploading a `.zip` of standard Gerber and Excellon `.drl` files, the app instantly generates exact 3D meshes using `manifold3d` and `trimesh`.

It produces two STLs:


1. **The Main Board:** A 3D model of your PCB with perfectly recessed channels for your traces and punched-through vias.


1. **The Companion Mold:** A stamping tool mathematically offset by your printer's tolerances, used to easily press copper tape down into the main board's recesses before sanding away the excess.



## Features


- **Browser-based 3D Viewer:** Inspect your generated STLs before printing using the integrated Three.js PTZ viewer.


- **Smart Scaling:** Automatically detects and corrects common unit-mismatch errors (e.g., Excellon files stuck in inches).


- **Customizable Tolerances:** Sliders for base thickness, trace depth, and printer margin offsets.


- **Python-Native Geometry:** Performs lightning-fast boolean operations in-memory without relying on external CAD software like OpenSCAD.



## Quick Start (Docker)

The easiest way to run PCB Press is via Docker. You don't need to configure Python environments or install C-extensions manually.


1. Clone the repository.


1. Run the following command in the project root:

```
docker-compose up --build   

```


1. Open your browser and navigate to `http://127.0.0.1:8000`.



*Note: Any 3D models generated via the web UI will automatically be saved to your local `static/models` directory.*