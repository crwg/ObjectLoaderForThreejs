# Three.js ObjectLoader Import/Export Blender plugin

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Blender](https://img.shields.io/badge/Blender-3.0%20--%204.x-orange.svg)](https://www.blender.org/)
[![Three.js](https://img.shields.io/badge/Three.js-JSON%20Format%204+-black.svg)](https://threejs.org/)

> **Direct JSON mesh exchange between Blender and Three.js.**  
> Optimized for geometry fidelity and workflow efficiency.

## ⚡ Capabilities

| Component | Support | Details |
| :--- | :---: | :--- |
| **Geometry** | ✅ **Full** | Vertices, UVs, Normals, Colors |
| **Materials** | ⚠️ **Partial** | Basic material properties (WIP) |
| **Hierarchy** | ✅ **Full** | Object transforms & parenting |
| **Scene Graph** | 🚧 **Beta** | Full scene import/export coming soon |

## 📦 Installation

1.  **Download** the latest `.zip` from [Releases](https://github.com/crwg/ObjectLoaderForThreejs/releases).
2.  **Blender:** `Edit` → `Preferences` → `Add-ons` → `Install...`.
3.  **Enable:** Search `ObjectLoader` and check the box.
4.  **Save:** Click `Save Preferences` to persist.

## 🛠 Usage

### Export Mesh
1.  Select target objects in the viewport.
2.  `File` → `Export` → `Three.js JSON (.json)`.
3.  Configure options (Apply modifiers, UVs, etc.).

### Import Mesh
1.  `File` → `Import` → `Three.js JSON (.json)`.
2.  Select file → Click `Import`.

## ⭐ Support the Project
## 📬 Stay Updated

Found this useful? [**Star the repo**](https://github.com/crwg/ObjectLoaderForThreejs) to support development and get notified about new releases.

## ⚙️ Technical Specifications

-   **Format:** Three.js JSON Object Scene Format (Version 4+).
-   **Coordinate System:** Auto-conversion (Y-up ↔ Z-up).
-   **Units:** Metric scaling support.
-   **Performance:** Optimized for large vertex counts.

## 🤝 Contributing

Scene graph completion is community-driven. Feel free to open Issues or PRs.

[![View Issues](https://img.shields.io/badge/View-Issues-red.svg)](https://github.com/crwg/ObjectLoaderForThreejs/issues)
[![Contribute](https://img.shields.io/badge/Contribute-PR-green.svg)](https://github.com/crwg/ObjectLoaderForThreejs/pulls)

---

**Author:** [crwg](https://github.com/crwg)  
**Repository:** `ObjectLoaderForThreejs`
