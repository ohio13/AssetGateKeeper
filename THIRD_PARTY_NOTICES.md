# Third-party code in this repository

AssetGatekeeper itself is MIT-licensed (see [LICENSE](LICENSE)). Two pieces of
other people's code ship inside it, unchanged, under their own MIT licences:

| What | Where | Licence |
| :--- | :--- | :--- |
| [three.js](https://github.com/mrdoob/three.js) r160: `three.module.js` and the add-ons OrbitControls, GLTFLoader, OBJLoader, MTLLoader, BufferGeometryUtils, ConvexGeometry and ConvexHull, used by the review page's 3D viewer | `server/static/vendor/` | MIT, © 2010-2023 three.js authors ([text](server/static/vendor/LICENSE)) |
| [tomli](https://github.com/hukkin/tomli), used to read `rules.toml` on Python 3.9 and 3.10 | `agk_vendor/tomli/` | MIT, © 2021 Taneli Hukkinen ([text](agk_vendor/tomli/LICENSE)) |

Everything else installed with it (trimesh, Pillow, numpy, DracoPy,
charset-normalizer, watchdog, fast-simplification, xatlas, scipy) comes from
PyPI under its own licence and is not copied into this repository.

The test models and textures in `tests/fixtures/` and the screenshots in
`docs/images/` were made for this project. Models from public collections that
some tests use are downloaded at test time, not stored here.
