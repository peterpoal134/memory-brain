# Memory Brain 3D renderer

The source in `brain/` keeps the original gold/violet cortical hologram and cyan
stage. `HologramScene.js` uses Three.js; the rest of the homepage is plain local
HTML/CSS/JavaScript in `xiaojiu_brain/static/brain.*`.

To rebuild from the project root:

```powershell
cd frontend
npm install
cd ..
node scripts/build_brain.mjs
```

The dependencies in `package.json` are pinned. The build bundles Three.js and its
used addons into `xiaojiu_brain/static/brain-renderer.js`; no CDN is requested at
runtime. Installing the Python wheel does not require Node.js because the bundle
and cortical assets are included.

The current cortical points represent authenticated memory document indices,
with time/type filters, hover previews and explicit source inspection. Their
positions are illustrative, not anatomical locations or verified semantic
relationships. Decorative filaments are not individual memories. Aggregate
feature navigation is displayed separately in the sidebar.

See `../NOTICE.md` and `../xiaojiu_brain/static/cortex/LICENSE-ATTRIBUTION.md` for
the separate CC BY-SA 3.0 cortical asset and MIT Three.js licenses. The bundled
assets must retain their own notices.
