import deckyPlugin from "@decky/rollup";

// Builds src/index.tsx to dist/index.js. React, react-dom and @decky/ui are provided by
// Steam / Decky Loader at runtime and are not bundled. Nothing under preview/ is reachable
// from src/index.tsx, so none of it ends up in the bundle.
export default deckyPlugin({});
