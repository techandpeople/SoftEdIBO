# 3D models

Layout rule: `<area>/<object>/<FORMAT>/<Part>.<ext>`

- `<FORMAT>`  STL (print-ready), STEP (exchange), F3D (Fusion 360 source), DXF (2D profiles / templates to sketch new models from)
- `<area>`    Enclosures | SkinMolds | Gateway
- `<object>`  the robot or part group (Thymio, Tree, Turtle, CommonBase, OrganBoxes, Shared)

A `.f3d` is a Fusion design and usually holds SEVERAL bodies, so one source
exports several parts. Exports are named by part, not by source:

    Enclosures/Thymio/F3D/ThymioSkinAdapter.f3d
      -> STL/ThymioSkinAdapter.stl, ThymioSkinHolder.stl, ThymioSkinCover.stl, ThymioLEDGuard.stl
      -> STEP/ (same names)

Files with a `-alt` / `-FIXME` suffix are unresolved variants.
