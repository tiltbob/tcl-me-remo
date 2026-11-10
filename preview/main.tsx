// The preview harness: renders the real plugin (src/index.tsx) in an ordinary browser.
// "@decky/ui" and "@decky/api" are replaced at bundle time by the stand-ins in mocks/, and
// the backend by fake-backend.ts. Nothing in src/ knows the difference.
//
//   ?scenario=default|unpaired|no-service|stopped|failed   state of the fake system
//   &path=/tcl-me-remo/input                               open a settings page (default: the panel)

import "./timers";

import type { FC } from "react";
import { useEffect } from "react";
import { createRoot } from "react-dom/client";

import plugin from "../src/index";
import { store } from "../src/store";
import { fake } from "./fake-backend";
import { dismissToast, getHost, navigate, record, setQuickAccessVisible, unloadPlugin, useHost } from "./host";
import { ModalHost } from "./mocks/decky-ui";
import { liveIntervals } from "./timers";

window.addEventListener("unhandledrejection", (event) => {
  console.error("unhandled rejection:", event.reason);
});

// What Decky Loader does when the Steam UI loads: call the default export once.
const definition = plugin();

const Toasts: FC = () => {
  const { toasts } = useHost();
  return (
    <div className="toasts">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={toast.critical ? "toast toast-critical" : "toast"}
          onClick={() => dismissToast(toast.id)}
        >
          <div className="toast-title">{toast.title}</div>
          <div className="toast-body">{toast.body}</div>
        </div>
      ))}
    </div>
  );
};

const App: FC = () => {
  const host = useHost();
  const base = Object.keys(host.routes).find((path) => host.location === path || host.location.startsWith(`${path}/`));
  const Route = base ? host.routes[base] : null;
  useEffect(() => {
    document.body.dataset.view = Route ? "route" : "qam";
  }, [Route]);
  if (!host.pluginLoaded) return <div className="qam">The plugin has been unloaded.</div>;
  return (
    <>
      {Route ? (
        <div className="bigpicture">
          <Route />
        </div>
      ) : (
        <div className="qam">
          <div className="qam-header">{definition.titleView}</div>
          {/* Steam keeps the panel mounted while the Quick Access Menu is closed and only
              hides it; the plugin learns about it through useQuickAccessVisible(). */}
          <div className="qam-content" hidden={!host.quickAccessVisible}>
            {definition.content}
          </div>
        </div>
      )}
      <ModalHost />
      <Toasts />
    </>
  );
};

const query = new URLSearchParams(window.location.search);
const first = query.get("path");
if (first) navigate(first);

const root = createRoot(document.getElementById("root") as HTMLElement);
root.render(<App />);

const preview = {
  fake,
  record,
  store,
  host: getHost,
  navigate,
  liveIntervals,
  /** Close the Quick Access Menu, as Steam does: the panel's content unmounts. */
  setQuickAccessVisible,
  /** Unmount everything, as when the Steam UI goes away. */
  unmount: () => root.unmount(),
  /** What the loader does when the plugin is unloaded: onDismount first, then its UI goes. */
  dismount: () => {
    definition.onDismount?.();
    unloadPlugin();
  },
};

declare global {
  interface Window {
    __preview: typeof preview;
  }
}
window.__preview = preview;
