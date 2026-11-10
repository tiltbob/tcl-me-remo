const manifest = {"name":"tcl-me-remo"};
const API_VERSION = 2;
const internalAPIConnection = window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit;
if (!internalAPIConnection) {
    throw new Error('[@decky/api]: Failed to connect to the loader as as the loader API was not initialized. This is likely a bug in Decky Loader.');
}
let api;
try {
    api = internalAPIConnection.connect(API_VERSION, manifest.name);
}
catch {
    api = internalAPIConnection.connect(1, manifest.name);
    console.warn(`[@decky/api] Requested API version ${API_VERSION} but the running loader only supports version 1. Some features may not work.`);
}
if (api._version != API_VERSION) {
    console.warn(`[@decky/api] Requested API version ${API_VERSION} but the running loader only supports version ${api._version}. Some features may not work.`);
}
const callable = api.callable;
const routerHook = api.routerHook;
const toaster = api.toaster;
const useQuickAccessVisible = api.useQuickAccessVisible;
const definePlugin = (fn) => {
    return (...args) => {
        return fn(...args);
    };
};

var DefaultContext = {
  color: undefined,
  size: undefined,
  className: undefined,
  style: undefined,
  attr: undefined
};
var IconContext = SP_REACT.createContext && /*#__PURE__*/SP_REACT.createContext(DefaultContext);

var _excluded = ["attr", "size", "title"];
function _objectWithoutProperties(e, t) { if (null == e) return {}; var o, r, i = _objectWithoutPropertiesLoose(e, t); if (Object.getOwnPropertySymbols) { var n = Object.getOwnPropertySymbols(e); for (r = 0; r < n.length; r++) o = n[r], -1 === t.indexOf(o) && {}.propertyIsEnumerable.call(e, o) && (i[o] = e[o]); } return i; }
function _objectWithoutPropertiesLoose(r, e) { if (null == r) return {}; var t = {}; for (var n in r) if ({}.hasOwnProperty.call(r, n)) { if (-1 !== e.indexOf(n)) continue; t[n] = r[n]; } return t; }
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function ownKeys(e, r) { var t = Object.keys(e); if (Object.getOwnPropertySymbols) { var o = Object.getOwnPropertySymbols(e); r && (o = o.filter(function (r) { return Object.getOwnPropertyDescriptor(e, r).enumerable; })), t.push.apply(t, o); } return t; }
function _objectSpread(e) { for (var r = 1; r < arguments.length; r++) { var t = null != arguments[r] ? arguments[r] : {}; r % 2 ? ownKeys(Object(t), true).forEach(function (r) { _defineProperty(e, r, t[r]); }) : Object.getOwnPropertyDescriptors ? Object.defineProperties(e, Object.getOwnPropertyDescriptors(t)) : ownKeys(Object(t)).forEach(function (r) { Object.defineProperty(e, r, Object.getOwnPropertyDescriptor(t, r)); }); } return e; }
function _defineProperty(e, r, t) { return (r = _toPropertyKey(r)) in e ? Object.defineProperty(e, r, { value: t, enumerable: true, configurable: true, writable: true }) : e[r] = t, e; }
function _toPropertyKey(t) { var i = _toPrimitive(t, "string"); return "symbol" == typeof i ? i : i + ""; }
function _toPrimitive(t, r) { if ("object" != typeof t || !t) return t; var e = t[Symbol.toPrimitive]; if (void 0 !== e) { var i = e.call(t, r); if ("object" != typeof i) return i; throw new TypeError("@@toPrimitive must return a primitive value."); } return ("string" === r ? String : Number)(t); }
function Tree2Element(tree) {
  return tree && tree.map((node, i) => /*#__PURE__*/SP_REACT.createElement(node.tag, _objectSpread({
    key: i
  }, node.attr), Tree2Element(node.child)));
}
function GenIcon(data) {
  return props => /*#__PURE__*/SP_REACT.createElement(IconBase, _extends({
    attr: _objectSpread({}, data.attr)
  }, props), Tree2Element(data.child));
}
function IconBase(props) {
  var elem = conf => {
    var attr = props.attr,
      size = props.size,
      title = props.title,
      svgProps = _objectWithoutProperties(props, _excluded);
    var computedSize = size || conf.size || "1em";
    var className;
    if (conf.className) className = conf.className;
    if (props.className) className = (className ? className + " " : "") + props.className;
    return /*#__PURE__*/SP_REACT.createElement("svg", _extends({
      stroke: "currentColor",
      fill: "currentColor",
      strokeWidth: "0"
    }, conf.attr, attr, svgProps, {
      className: className,
      style: _objectSpread(_objectSpread({
        color: props.color || conf.color
      }, conf.style), props.style),
      height: computedSize,
      width: computedSize,
      xmlns: "http://www.w3.org/2000/svg"
    }), title && /*#__PURE__*/SP_REACT.createElement("title", null, title), props.children);
  };
  return IconContext !== undefined ? /*#__PURE__*/SP_REACT.createElement(IconContext.Consumer, null, conf => elem(conf)) : elem(DefaultContext);
}

// THIS FILE IS AUTO GENERATED
function FaVolumeUp (props) {
  return GenIcon({"attr":{"viewBox":"0 0 576 512"},"child":[{"tag":"path","attr":{"d":"M215.03 71.05L126.06 160H24c-13.26 0-24 10.74-24 24v144c0 13.25 10.74 24 24 24h102.06l88.97 88.95c15.03 15.03 40.97 4.47 40.97-16.97V88.02c0-21.46-25.96-31.98-40.97-16.97zm233.32-51.08c-11.17-7.33-26.18-4.24-33.51 6.95-7.34 11.17-4.22 26.18 6.95 33.51 66.27 43.49 105.82 116.6 105.82 195.58 0 78.98-39.55 152.09-105.82 195.58-11.17 7.32-14.29 22.34-6.95 33.5 7.04 10.71 21.93 14.56 33.51 6.95C528.27 439.58 576 351.33 576 256S528.27 72.43 448.35 19.97zM480 256c0-63.53-32.06-121.94-85.77-156.24-11.19-7.14-26.03-3.82-33.12 7.46s-3.78 26.21 7.41 33.36C408.27 165.97 432 209.11 432 256s-23.73 90.03-63.48 115.42c-11.19 7.14-14.5 22.07-7.41 33.36 6.51 10.36 21.12 15.14 33.12 7.46C447.94 377.94 480 319.54 480 256zm-141.77-76.87c-11.58-6.33-26.19-2.16-32.61 9.45-6.39 11.61-2.16 26.2 9.45 32.61C327.98 228.28 336 241.63 336 256c0 14.38-8.02 27.72-20.92 34.81-11.61 6.41-15.84 21-9.45 32.61 6.43 11.66 21.05 15.8 32.61 9.45 28.23-15.55 45.77-45 45.77-76.88s-17.54-61.32-45.78-76.86z"},"child":[]}]})(props);
}function FaVolumeMute (props) {
  return GenIcon({"attr":{"viewBox":"0 0 512 512"},"child":[{"tag":"path","attr":{"d":"M215.03 71.05L126.06 160H24c-13.26 0-24 10.74-24 24v144c0 13.25 10.74 24 24 24h102.06l88.97 88.95c15.03 15.03 40.97 4.47 40.97-16.97V88.02c0-21.46-25.96-31.98-40.97-16.97zM461.64 256l45.64-45.64c6.3-6.3 6.3-16.52 0-22.82l-22.82-22.82c-6.3-6.3-16.52-6.3-22.82 0L416 210.36l-45.64-45.64c-6.3-6.3-16.52-6.3-22.82 0l-22.82 22.82c-6.3 6.3-6.3 16.52 0 22.82L370.36 256l-45.63 45.63c-6.3 6.3-6.3 16.52 0 22.82l22.82 22.82c6.3 6.3 16.52 6.3 22.82 0L416 301.64l45.64 45.64c6.3 6.3 16.52 6.3 22.82 0l22.82-22.82c6.3-6.3 6.3-16.52 0-22.82L461.64 256z"},"child":[]}]})(props);
}function FaVolumeDown (props) {
  return GenIcon({"attr":{"viewBox":"0 0 384 512"},"child":[{"tag":"path","attr":{"d":"M215.03 72.04L126.06 161H24c-13.26 0-24 10.74-24 24v144c0 13.25 10.74 24 24 24h102.06l88.97 88.95c15.03 15.03 40.97 4.47 40.97-16.97V89.02c0-21.47-25.96-31.98-40.97-16.98zm123.2 108.08c-11.58-6.33-26.19-2.16-32.61 9.45-6.39 11.61-2.16 26.2 9.45 32.61C327.98 229.28 336 242.62 336 257c0 14.38-8.02 27.72-20.92 34.81-11.61 6.41-15.84 21-9.45 32.61 6.43 11.66 21.05 15.8 32.61 9.45 28.23-15.55 45.77-45 45.77-76.88s-17.54-61.32-45.78-76.87z"},"child":[]}]})(props);
}function FaTv (props) {
  return GenIcon({"attr":{"viewBox":"0 0 640 512"},"child":[{"tag":"path","attr":{"d":"M592 0H48A48 48 0 0 0 0 48v320a48 48 0 0 0 48 48h240v32H112a16 16 0 0 0-16 16v32a16 16 0 0 0 16 16h416a16 16 0 0 0 16-16v-32a16 16 0 0 0-16-16H352v-32h240a48 48 0 0 0 48-48V48a48 48 0 0 0-48-48zm-16 352H64V64h512z"},"child":[]}]})(props);
}function FaHome (props) {
  return GenIcon({"attr":{"viewBox":"0 0 576 512"},"child":[{"tag":"path","attr":{"d":"M280.37 148.26L96 300.11V464a16 16 0 0 0 16 16l112.06-.29a16 16 0 0 0 15.92-16V368a16 16 0 0 1 16-16h64a16 16 0 0 1 16 16v95.64a16 16 0 0 0 16 16.05L464 480a16 16 0 0 0 16-16V300L295.67 148.26a12.19 12.19 0 0 0-15.3 0zM571.6 251.47L488 182.56V44.05a12 12 0 0 0-12-12h-56a12 12 0 0 0-12 12v72.61L318.47 43a48 48 0 0 0-61 0L4.34 251.47a12 12 0 0 0-1.6 16.9l25.5 31A12 12 0 0 0 45.15 301l235.22-193.74a12.19 12.19 0 0 1 15.3 0L530.9 301a12 12 0 0 0 16.9-1.6l25.5-31a12 12 0 0 0-1.7-16.93z"},"child":[]}]})(props);
}function FaExclamationTriangle (props) {
  return GenIcon({"attr":{"viewBox":"0 0 576 512"},"child":[{"tag":"path","attr":{"d":"M569.517 440.013C587.975 472.007 564.806 512 527.94 512H48.054c-36.937 0-59.999-40.055-41.577-71.987L246.423 23.985c18.467-32.009 64.72-31.951 83.154 0l239.94 416.028zM288 354c-25.405 0-46 20.595-46 46s20.595 46 46 46 46-20.595 46-46-20.595-46-46-46zm-43.673-165.346l7.418 136c.347 6.364 5.609 11.346 11.982 11.346h48.546c6.373 0 11.635-4.982 11.982-11.346l7.418-136c.375-6.874-5.098-12.654-11.982-12.654h-63.383c-6.884 0-12.356 5.78-11.981 12.654z"},"child":[]}]})(props);
}function FaCog (props) {
  return GenIcon({"attr":{"viewBox":"0 0 512 512"},"child":[{"tag":"path","attr":{"d":"M487.4 315.7l-42.6-24.6c4.3-23.2 4.3-47 0-70.2l42.6-24.6c4.9-2.8 7.1-8.6 5.5-14-11.1-35.6-30-67.8-54.7-94.6-3.8-4.1-10-5.1-14.8-2.3L380.8 110c-17.9-15.4-38.5-27.3-60.8-35.1V25.8c0-5.6-3.9-10.5-9.4-11.7-36.7-8.2-74.3-7.8-109.2 0-5.5 1.2-9.4 6.1-9.4 11.7V75c-22.2 7.9-42.8 19.8-60.8 35.1L88.7 85.5c-4.9-2.8-11-1.9-14.8 2.3-24.7 26.7-43.6 58.9-54.7 94.6-1.7 5.4.6 11.2 5.5 14L67.3 221c-4.3 23.2-4.3 47 0 70.2l-42.6 24.6c-4.9 2.8-7.1 8.6-5.5 14 11.1 35.6 30 67.8 54.7 94.6 3.8 4.1 10 5.1 14.8 2.3l42.6-24.6c17.9 15.4 38.5 27.3 60.8 35.1v49.2c0 5.6 3.9 10.5 9.4 11.7 36.7 8.2 74.3 7.8 109.2 0 5.5-1.2 9.4-6.1 9.4-11.7v-49.2c22.2-7.9 42.8-19.8 60.8-35.1l42.6 24.6c4.9 2.8 11 1.9 14.8-2.3 24.7-26.7 43.6-58.9 54.7-94.6 1.5-5.5-.7-11.3-5.6-14.1zM256 336c-44.1 0-80-35.9-80-80s35.9-80 80-80 80 35.9 80 80-35.9 80-80 80z"},"child":[]}]})(props);
}function FaChevronUp (props) {
  return GenIcon({"attr":{"viewBox":"0 0 448 512"},"child":[{"tag":"path","attr":{"d":"M240.971 130.524l194.343 194.343c9.373 9.373 9.373 24.569 0 33.941l-22.667 22.667c-9.357 9.357-24.522 9.375-33.901.04L224 227.495 69.255 381.516c-9.379 9.335-24.544 9.317-33.901-.04l-22.667-22.667c-9.373-9.373-9.373-24.569 0-33.941L207.03 130.525c9.372-9.373 24.568-9.373 33.941-.001z"},"child":[]}]})(props);
}function FaChevronRight (props) {
  return GenIcon({"attr":{"viewBox":"0 0 320 512"},"child":[{"tag":"path","attr":{"d":"M285.476 272.971L91.132 467.314c-9.373 9.373-24.569 9.373-33.941 0l-22.667-22.667c-9.357-9.357-9.375-24.522-.04-33.901L188.505 256 34.484 101.255c-9.335-9.379-9.317-24.544.04-33.901l22.667-22.667c9.373-9.373 24.569-9.373 33.941 0L285.475 239.03c9.373 9.372 9.373 24.568.001 33.941z"},"child":[]}]})(props);
}function FaChevronLeft (props) {
  return GenIcon({"attr":{"viewBox":"0 0 320 512"},"child":[{"tag":"path","attr":{"d":"M34.52 239.03L228.87 44.69c9.37-9.37 24.57-9.37 33.94 0l22.67 22.67c9.36 9.36 9.37 24.52.04 33.9L131.49 256l154.02 154.75c9.34 9.38 9.32 24.54-.04 33.9l-22.67 22.67c-9.37 9.37-24.57 9.37-33.94 0L34.52 272.97c-9.37-9.37-9.37-24.57 0-33.94z"},"child":[]}]})(props);
}function FaChevronDown (props) {
  return GenIcon({"attr":{"viewBox":"0 0 448 512"},"child":[{"tag":"path","attr":{"d":"M207.029 381.476L12.686 187.132c-9.373-9.373-9.373-24.569 0-33.941l22.667-22.667c9.357-9.357 24.522-9.375 33.901-.04L224 284.505l154.745-154.021c9.379-9.335 24.544-9.317 33.901.04l22.667 22.667c9.373 9.373 9.373 24.569 0 33.941L240.971 381.476c-9.373 9.372-24.569 9.372-33.942 0z"},"child":[]}]})(props);
}function FaCheckCircle (props) {
  return GenIcon({"attr":{"viewBox":"0 0 512 512"},"child":[{"tag":"path","attr":{"d":"M504 256c0 136.967-111.033 248-248 248S8 392.967 8 256 119.033 8 256 8s248 111.033 248 248zM227.314 387.314l184-184c6.248-6.248 6.248-16.379 0-22.627l-22.627-22.627c-6.248-6.249-16.379-6.249-22.628 0L216 308.118l-70.059-70.059c-6.248-6.248-16.379-6.248-22.628 0l-22.627 22.627c-6.248 6.248-6.248 16.379 0 22.627l104 104c6.249 6.249 16.379 6.249 22.628.001z"},"child":[]}]})(props);
}function FaBars (props) {
  return GenIcon({"attr":{"viewBox":"0 0 448 512"},"child":[{"tag":"path","attr":{"d":"M16 132h416c8.837 0 16-7.163 16-16V76c0-8.837-7.163-16-16-16H16C7.163 60 0 67.163 0 76v40c0 8.837 7.163 16 16 16zm0 160h416c8.837 0 16-7.163 16-16v-40c0-8.837-7.163-16-16-16H16c-8.837 0-16 7.163-16 16v40c0 8.837 7.163 16 16 16zm0 160h416c8.837 0 16-7.163 16-16v-40c0-8.837-7.163-16-16-16H16c-8.837 0-16 7.163-16 16v40c0 8.837 7.163 16 16 16z"},"child":[]}]})(props);
}function FaArrowLeft (props) {
  return GenIcon({"attr":{"viewBox":"0 0 448 512"},"child":[{"tag":"path","attr":{"d":"M257.5 445.1l-22.2 22.2c-9.4 9.4-24.6 9.4-33.9 0L7 273c-9.4-9.4-9.4-24.6 0-33.9L201.4 44.7c9.4-9.4 24.6-9.4 33.9 0l22.2 22.2c9.5 9.5 9.3 25-.4 34.3L136.6 216H424c13.3 0 24 10.7 24 24v32c0 13.3-10.7 24-24 24H136.6l120.5 114.8c9.8 9.3 10 24.8.4 34.3z"},"child":[]}]})(props);
}function FaRegCircle (props) {
  return GenIcon({"attr":{"viewBox":"0 0 512 512"},"child":[{"tag":"path","attr":{"d":"M256 8C119 8 8 119 8 256s111 248 248 248 248-111 248-248S393 8 256 8zm0 448c-110.5 0-200-89.5-200-200S145.5 56 256 56s200 89.5 200 200-89.5 200-200 200z"},"child":[]}]})(props);
}

// The only place that talks to the plugin backend (main.py).
//
// Every function here resolves to an RpcReply and never rejects: a backend that throws, a
// reply of the wrong shape and a call that is never answered (Decky does not settle calls
// that are in flight when the backend stops) all become `{ok: false, error}`.
const callRpc = callable("rpc");
const callServiceInfo = callable("service_info");
const callInstallService = callable("install_service");
const callRemoveService = callable("remove_service");
const callRestartService = callable("restart_service");
const callDoctor = callable("doctor");
const callServiceLog = callable("service_log");
// Safety nets only. main.py gives up first (its TIMEOUTS table: 240 s at most for the slow
// methods, 45 s or less for the rest); these fire when the backend itself has gone away.
const SLOW_METHODS = new Set(["tv_on", "switch_input", "run_macro", "event", "connect"]);
const SLOW_SECONDS = 260;
const NORMAL_SECONDS = 60;
const STATUS_SECONDS = 20;
// install_service and remove_service wait for an install that is already running (main.py
// installs the service in the background when the plugin loads; up to 120 s) and then run
// their own (up to another 120 s).
const INSTALLER_SECONDS = 260;
// The doctor asks a dozen system tools, each with its own time limit; main.py gives it 75 s.
const DOCTOR_SECONDS = 90;
function failure(code, message) {
    return { ok: false, error: { code, message } };
}
function describe(reason) {
    // A Python exception arrives with an empty message (see the Decky notes), so do not
    // pretend to know more than "it failed".
    const text = reason instanceof Error ? reason.message : typeof reason === "string" ? reason : "";
    return text ? `the plugin backend failed: ${text}` : "the plugin backend failed (details are in the Decky log)";
}
function isObject$1(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}
function asReply(value) {
    if (isObject$1(value) && value.ok === true) {
        return { ok: true, result: value.result };
    }
    if (isObject$1(value) && value.ok === false) {
        const error = isObject$1(value.error) ? value.error : {};
        return failure(String(error.code ?? "error"), String(error.message ?? "unknown error"));
    }
    return failure("bad_reply", "the plugin backend sent an unreadable reply");
}
function settle(what, seconds, start, read = asReply) {
    return new Promise((resolve) => {
        let done = false;
        const finish = (reply) => {
            if (done)
                return;
            done = true;
            clearTimeout(timer);
            resolve(reply);
        };
        const timer = setTimeout(() => finish(failure("timeout", `the plugin backend did not answer '${what}' within ${seconds}s`)), seconds * 1000);
        try {
            start().then((value) => {
                let reply;
                try {
                    reply = read(value);
                }
                catch (reason) {
                    reply = failure("bad_reply", describe(reason));
                }
                finish(reply);
            }, (reason) => finish(failure("plugin_error", describe(reason))));
        }
        catch (reason) {
            finish(failure("plugin_error", describe(reason)));
        }
    });
}
/** One request to the service (docs/control-protocol.md). */
function rpc(method, params = {}) {
    const seconds = method === "status" ? STATUS_SECONDS : SLOW_METHODS.has(method) ? SLOW_SECONDS : NORMAL_SECONDS;
    return settle(method, seconds, () => callRpc(method, params));
}
/** What is installed and whether it runs. Works when the service itself does not. */
function serviceInfo() {
    return settle("service_info", NORMAL_SECONDS, callServiceInfo, (value) => {
        if (isObject$1(value) && typeof value.installed === "boolean") {
            const problems = Array.isArray(value.problems) ? value.problems.map(String) : [];
            return { ok: true, result: { ...value, problems } };
        }
        return failure("bad_reply", "the plugin backend sent an unreadable reply");
    });
}
function installService$1() {
    return settle("install_service", INSTALLER_SECONDS, callInstallService);
}
function removeService$1(purge) {
    return settle("remove_service", INSTALLER_SECONDS, () => callRemoveService(purge));
}
function restartService$1() {
    return settle("restart_service", 90, callRestartService);
}
/** The report of "tcl-me-remo doctor". Read-only; works when the service itself does not. */
function doctor() {
    return settle("doctor", DOCTOR_SECONDS, callDoctor, (value) => {
        const reply = asReply(value);
        if (!reply.ok)
            return reply;
        const result = reply.result;
        if (!isObject$1(result) || !Array.isArray(result.checks)) {
            return failure("bad_reply", "the plugin backend sent an unreadable report");
        }
        const lines = (list) => (Array.isArray(list) ? list.map(String) : []);
        return {
            ok: true,
            result: {
                worst: String(result.worst ?? "unknown"),
                system: lines(result.system),
                checks: result.checks.filter(isObject$1).map((check) => ({
                    name: String(check.name ?? ""),
                    status: String(check.status ?? "unknown"),
                    detail: String(check.detail ?? ""),
                })),
                details: lines(result.details),
            },
        };
    });
}
/**
 * The service's lines in the system journal, oldest first. For when the service does not
 * answer; while it does, its own "logs" request is the better source.
 */
function serviceLog(lines) {
    return settle("service_log", NORMAL_SECONDS, () => callServiceLog(lines));
}

// Wording shared by the panel and the settings pages.
const colors = {
    ok: "#59bf40",
    warn: "#ffc82c",
    bad: "#ff6b6b",
    badBackground: "rgba(209, 28, 28, 0.22)",
    dim: "#8b929a",
    text: "#dcdedf",
};
/**
 * Where the plugin is installed from, and updated from: typed into "Install Plugin from URL"
 * on Decky's Developer page. Without "https://", which Decky adds itself. The last part must
 * stay "<the name in plugin.json>.zip": Decky tells an update from a fresh install by it
 * (tests/test_release_plan.py holds the README, the release workflow and this to that).
 */
const UPDATE_ADDRESS = "github.com/tiltbob/tcl-me-remo/releases/latest/download/tcl-me-remo.zip";
const kindColor = {
    ok: colors.ok,
    skipped: colors.warn,
    failed: colors.bad,
};
const kindWord = {
    ok: "done",
    skipped: "skipped",
    failed: "failed",
};
const ACTION_NAMES = {
    tv_on: "TV on",
    tv_off: "TV off",
    switch_input: "Switch input",
    macro: "Macro",
};
const TRIGGER_NAMES = {
    boot: "start",
    resume: "wake",
    suspend: "suspend",
    shutdown: "shutdown",
    reboot: "restart",
    manual: "manual",
};
/** The service's `busy` text ("tv_on (boot)") in the panel's words ("TV on (start)"). */
function busyName(busy) {
    const match = /^(\w+) \((\w+)\)$/.exec(busy);
    if (!match)
        return busy;
    return `${ACTION_NAMES[match[1]] ?? match[1]} (${TRIGGER_NAMES[match[2]] ?? match[2]})`;
}
/**
 * A refused request in the panel's words. The service says "busy with 'tv_on' (resume); try
 * again shortly"; everything else is already plain.
 */
function refusalText(code, message) {
    if (code !== "busy")
        return message;
    const match = /'(\w+)' \((\w+)\)/.exec(message);
    if (!match)
        return message;
    return `Busy with ${busyName(`${match[1]} (${match[2]})`)}. Try again when that has finished.`;
}
function actionName(record) {
    return ACTION_NAMES[record.name] ?? String(record.name);
}
function triggerName(record) {
    return TRIGGER_NAMES[record.trigger] ?? String(record.trigger);
}
/** "just now", "3 min ago", "2 h ago", or a date for older things. `time` is in seconds. */
function timeAgo(time, now = Date.now()) {
    const seconds = Math.max(0, now / 1000 - time);
    if (seconds < 20)
        return "just now";
    if (seconds < 90)
        return "a minute ago";
    if (seconds < 3600)
        return `${Math.round(seconds / 60)} min ago`;
    if (seconds < 36 * 3600)
        return `${Math.round(seconds / 3600)} h ago`;
    return new Date(time * 1000).toLocaleDateString();
}
/** The time of day, with the date in front when it was not today. `time` is in seconds. */
function clockTime(time, now = Date.now()) {
    const when = new Date(time * 1000);
    const today = when.toDateString() === new Date(now).toDateString();
    return today ? when.toLocaleTimeString() : `${when.toLocaleDateString()} ${when.toLocaleTimeString()}`;
}
function seconds(value) {
    return `${Math.round(value * 10) / 10} s`;
}
function duration(value) {
    if (value < 90)
        return `${Math.round(value)} s`;
    if (value < 5400)
        return `${Math.round(value / 60)} min`;
    if (value < 36 * 3600)
        return `${Math.round(value / 360) / 10} h`;
    return `${Math.round(value / 8640) / 10} days`;
}

// The shared data layer.
//
// One store serves the Quick Access panel, the settings page and every dialog:
//
//   * it polls the service's status about once a second while at least one of them is
//     mounted, and stops when none is (acquire() / the function it returns);
//   * a status that may be out of date is never passed off as current: when polling resumes
//     after a pause, or a request hangs, `checking` is set until the service has answered;
//   * it loads the settings once and after anything that can change them;
//   * setConfig() applies a change at once, sends it, adopts what the service answers and
//     takes the change back (with the service's message) when it is rejected;
//   * run() tracks an action from button press to outcome, so a button stays disabled while
//     its request is in flight and the result is still there when the user comes back.
//
// Nothing in here throws or rejects: a failed call is a normal state with a message.

const POLL_MS = 1000;
// service_info() runs the installer's status check (a subprocess and a few systemctl calls),
// so while the service is unreachable it is refreshed at this slower pace.
const SERVICE_INFO_MS = 5000;
const CONFIG_RESYNC_MS = 3000;
// A status older than this is not shown as the current state.
const STALE_MS = 3000;
// What service_info() said is trusted for this long after the service stops answering.
const SERVICE_FRESH_MS = 2000;
/** Outcome code of a second press that arrived while the first was still in flight. */
const ALREADY_RUNNING = "already_running";
const INITIAL = {
    started: false,
    checking: false,
    status: null,
    statusError: null,
    statusAsOf: 0,
    settling: false,
    service: null,
    serviceError: "",
    bundle: null,
    config: null,
    configError: "",
    configLoadError: "",
    pending: {},
    outcomes: {},
};
function isPlainObject(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}
function merge(base, patch) {
    if (!isPlainObject(base) || !isPlainObject(patch))
        return patch;
    const out = { ...base };
    for (const [key, value] of Object.entries(patch)) {
        out[key] = key in base ? merge(base[key], value) : value;
    }
    return out;
}
/**
 * Whether a status has every part the UI reads. An older service that is about to be updated
 * may answer with less; that must become a message, not a crash in the middle of rendering.
 */
function isStatus(value) {
    if (!isPlainObject(value))
        return false;
    const sections = ["bluetooth", "wifi", "display", "input"].every((name) => isPlainObject(value[name]));
    return (sections &&
        typeof value.busy === "string" &&
        Array.isArray(value.history) &&
        Array.isArray(value.config_problems) &&
        Array.isArray(value.display.connectors));
}
function isBundle(value) {
    if (!isPlainObject(value) || !isPlainObject(value.config) || !isPlainObject(value.schema))
        return false;
    const config = value.config;
    return ["events", "display", "power", "input", "bluetooth", "wifi"].every((name) => isPlainObject(config[name]));
}
const list = (value) => (Array.isArray(value) ? value : []);
function isActionRecord(value) {
    return (isPlainObject(value) &&
        typeof value.ok === "boolean" &&
        typeof value.detail === "string" &&
        Array.isArray(value.steps));
}
function recordKind(record) {
    return !record.ok ? "failed" : record.skipped ? "skipped" : "ok";
}
/** Keys of actions that occupy the TV; the service runs one of them at a time. */
function isTvAction(key) {
    return key.startsWith("tv.");
}
/** What is keeping the TV busy right now ("" when nothing is). */
function busyReason(state) {
    if (state.status?.busy)
        return busyName(state.status.busy);
    for (const [key, title] of Object.entries(state.pending)) {
        if (isTvAction(key))
            return title;
    }
    return "";
}
/** Whether the button for `key` must be disabled: its own request, or a busy service. */
function actionDisabled(state, key) {
    return state.checking || key in state.pending || busyReason(state) !== "";
}
class TvStore {
    constructor() {
        this.state = INITIAL;
        this.listeners = new Set();
        this.holders = 0;
        // Raised by shutdown(), so that holds handed out before it cannot be given back twice.
        this.generation = 0;
        this.timer = null;
        this.polling = false;
        this.pollAgain = false;
        this.pollStartedAt = 0;
        // Counts changes to the settings the service has; a status knows which count it was asked at.
        this.configSeq = 0;
        this.statusSeq = 0;
        this.serviceChecking = false;
        this.serviceCheckedAt = 0;
        this.configLoading = false;
        this.configLoadedAt = 0;
        this.resyncedFor = "";
        // Settings as last confirmed by the service, and the changes sent but not yet answered.
        this.confirmed = null;
        this.patches = [];
        this.configQueue = Promise.resolve();
    }
    getState() {
        return this.state;
    }
    subscribe(listener) {
        this.listeners.add(listener);
        return () => {
            this.listeners.delete(listener);
        };
    }
    update(partial) {
        const settling = this.patches.length > 0 || this.statusSeq < this.configSeq;
        this.state = { ...this.state, ...partial, settling };
        for (const listener of Array.from(this.listeners)) {
            try {
                listener(this.state);
            }
            catch (reason) {
                console.warn("tcl-me-remo: a listener failed", reason);
            }
        }
    }
    // ------------------------------------------------------------------ polling
    /** Start (or keep) polling. Returns the function that gives the hold back. */
    acquire() {
        const generation = this.generation;
        this.holders += 1;
        if (this.holders === 1) {
            // What is still here from before the pause is history, not the current state.
            if (this.state.status && Date.now() - this.state.statusAsOf > STALE_MS)
                this.update({ checking: true });
            this.timer = setInterval(() => this.tick(), POLL_MS);
            this.poll();
            if (!this.state.bundle)
                void this.loadConfig();
            if (!this.state.service)
                void this.refreshService();
        }
        let released = false;
        return () => {
            // A hold from before shutdown() was already given back by it.
            if (released || generation !== this.generation)
                return;
            released = true;
            this.holders -= 1;
            if (this.holders === 0 && this.timer !== null) {
                clearInterval(this.timer);
                this.timer = null;
            }
        };
    }
    /** Number of mounted users; the preview's tests read it. */
    get activeHolders() {
        return this.holders;
    }
    /** The once-a-second tick: skipped while the previous request has not returned. */
    tick() {
        if (!this.polling) {
            this.poll();
        }
        else if (this.state.status && !this.state.checking && Date.now() - this.pollStartedAt > STALE_MS) {
            // The service has stopped answering in time; what is shown may no longer be true.
            this.update({ checking: true });
        }
    }
    /**
     * Ask for the status now. Requests never overlap: while one is in flight, this one is
     * made as soon as it returns (something has changed that the one in flight may have missed).
     */
    poll() {
        if (this.polling) {
            this.pollAgain = true;
            return;
        }
        this.polling = true;
        this.pollAgain = false;
        this.pollStartedAt = Date.now();
        const askedAt = this.pollStartedAt;
        const seq = this.configSeq;
        const generation = this.generation;
        rpc("status")
            .then((reply) => {
            if (generation === this.generation)
                this.adoptStatus(reply, askedAt, seq);
        })
            .catch((reason) => console.warn("tcl-me-remo: status handling failed", reason))
            .finally(() => {
            this.polling = false;
            if (this.pollAgain && generation === this.generation)
                this.poll();
        });
    }
    adoptStatus(reply, askedAt, seq) {
        if (reply.ok && !isStatus(reply.result)) {
            reply = failure("bad_reply", "the service answers, but not in a form this version of the panel understands; Repair updates the service");
        }
        if (!reply.ok) {
            // Do not keep showing the last good status: it is no longer known to be true.
            const wentDown = this.state.status !== null || !this.state.started;
            const partial = {
                started: true,
                checking: false,
                status: null,
                statusError: reply.error,
                statusAsOf: 0,
            };
            // Nor what was installed and running back when it still answered.
            const infoIsOld = !this.serviceChecking && Date.now() - this.serviceCheckedAt > SERVICE_FRESH_MS;
            if (wentDown && infoIsOld)
                partial.service = null;
            this.update(partial);
            if ((wentDown && infoIsOld) || Date.now() - this.serviceCheckedAt >= SERVICE_INFO_MS)
                void this.refreshService();
            return;
        }
        const wasDown = this.state.started && this.state.status === null;
        this.statusSeq = seq;
        this.update({ started: true, checking: false, status: reply.result, statusError: null, statusAsOf: askedAt });
        if (wasDown) {
            // The service is back: what we knew about it may be out of date.
            void this.loadConfig();
            void this.refreshService();
        }
        else if (!this.state.bundle) {
            void this.loadConfig();
        }
        else {
            this.resyncIfChangedElsewhere(reply.result);
        }
    }
    /** Reload the settings when the status shows they were changed behind our back (CLI, pairing). */
    resyncIfChangedElsewhere(status) {
        const cfg = this.confirmed;
        if (!cfg || this.patches.length > 0 || this.configLoading)
            return;
        const seen = [
            status.enabled,
            status.input?.method,
            status.input?.hdmi_port,
            status.wifi?.enabled,
            status.wifi?.host,
            status.bluetooth?.paired ? status.bluetooth.tv_address : "",
        ].join("|");
        const known = [
            cfg.enabled,
            cfg.input.method,
            cfg.input.hdmi_port,
            cfg.wifi.enabled,
            cfg.wifi.host,
            cfg.bluetooth.tv_address,
        ].join("|");
        if (seen === known) {
            this.resyncedFor = "";
            return;
        }
        // Reload once per distinct difference, so two views that never agree cannot cause a loop.
        if (seen === this.resyncedFor || Date.now() - this.configLoadedAt < CONFIG_RESYNC_MS)
            return;
        this.resyncedFor = seen;
        void this.loadConfig();
    }
    // ------------------------------------------------------------------ service state
    refreshService() {
        if (this.serviceChecking)
            return Promise.resolve();
        this.serviceChecking = true;
        this.serviceCheckedAt = Date.now();
        return serviceInfo()
            .then((reply) => {
            if (reply.ok)
                this.update({ service: reply.result, serviceError: "" });
            else
                this.update({ serviceError: reply.error.message });
        })
            .catch((reason) => console.warn("tcl-me-remo: service_info handling failed", reason))
            .finally(() => {
            this.serviceChecking = false;
            this.serviceCheckedAt = Date.now();
        });
    }
    // ------------------------------------------------------------------ settings
    shownConfig() {
        if (!this.confirmed)
            return null;
        return this.patches.reduce((cfg, patch) => merge(cfg, patch), this.confirmed);
    }
    loadConfig() {
        if (this.configLoading)
            return Promise.resolve();
        this.configLoading = true;
        return rpc("get_config")
            .then((reply) => {
            this.configLoadedAt = Date.now();
            if (reply.ok && isBundle(reply.result)) {
                const bundle = {
                    ...reply.result,
                    presets: list(reply.result.presets),
                    keys: list(reply.result.keys),
                    link_presets: list(reply.result.link_presets),
                };
                if (JSON.stringify(bundle.config) !== JSON.stringify(this.confirmed))
                    this.configSeq += 1;
                this.confirmed = bundle.config;
                this.update({ bundle, config: this.shownConfig(), configLoadError: "" });
                if (this.state.settling)
                    this.poll();
            }
            else if (!reply.ok) {
                this.update({ configLoadError: reply.error.message });
            }
            else {
                this.update({ configLoadError: "the service sent settings this panel cannot read" });
            }
        })
            .catch((reason) => console.warn("tcl-me-remo: get_config handling failed", reason))
            .finally(() => {
            this.configLoading = false;
        });
    }
    /**
     * Change settings. The change shows at once; if the service rejects it, it is taken back
     * and `configError` carries the service's message, which names the setting.
     */
    setConfig(patch) {
        if (!this.confirmed) {
            const message = "the settings have not been loaded yet";
            this.update({ configError: message });
            return Promise.resolve({ ok: false, message });
        }
        this.patches.push(patch);
        this.update({ config: this.shownConfig() });
        // One request at a time, in the order the user made the changes.
        const result = this.configQueue.then(() => this.sendPatch(patch));
        this.configQueue = result.catch(() => undefined);
        return result.catch((reason) => {
            console.warn("tcl-me-remo: set_config handling failed", reason);
            return { ok: false, message: "the change could not be sent" };
        });
    }
    async sendPatch(patch) {
        let reply;
        try {
            reply = await rpc("set_config", { patch });
        }
        finally {
            this.patches = this.patches.filter((entry) => entry !== patch);
        }
        if (reply.ok && isBundle({ config: reply.result?.config, schema: {} })) {
            this.confirmed = reply.result.config;
            this.configLoadedAt = Date.now();
            this.configSeq += 1;
            this.update({ config: this.shownConfig(), configError: "" });
            this.poll();
            return { ok: true };
        }
        const message = reply.ok ? "the service sent an unreadable reply" : reply.error.message;
        const unchanged = !reply.ok && reply.error.code === "invalid_config";
        if (!unchanged)
            this.configSeq += 1;
        // Removing the patch above is the rollback: `config` falls back to what was confirmed.
        this.update({ config: this.shownConfig(), configError: message });
        if (!unchanged) {
            // "save_failed" means the change IS in effect; for anything else we cannot know.
            void this.loadConfig();
            this.poll();
        }
        return { ok: false, message };
    }
    clearConfigError() {
        if (this.state.configError)
            this.update({ configError: "" });
    }
    // ------------------------------------------------------------------ actions
    /**
     * Run one action and record how it went. A second press while the first is in flight is
     * ignored (the returned promise resolves with what is known so far).
     */
    async run(key, title, call, read = () => ({ text: "Done" })) {
        if (key in this.state.pending) {
            return this.outcome(key, title, "skipped", "this is already running", ALREADY_RUNNING, null);
        }
        const { [key]: _old, ...keptOutcomes } = this.state.outcomes;
        this.update({ pending: { ...this.state.pending, [key]: title }, outcomes: keptOutcomes });
        let outcome;
        try {
            const reply = await call();
            if (!reply.ok) {
                const text = refusalText(reply.error.code, reply.error.message);
                outcome = this.outcome(key, title, "failed", text, reply.error.code, null);
            }
            else if (isActionRecord(reply.result)) {
                const record = reply.result;
                outcome = this.outcome(key, title, recordKind(record), record.detail, "", record);
            }
            else {
                const { kind = "ok", text } = read(reply.result);
                outcome = this.outcome(key, title, kind, text, "", null);
            }
        }
        catch (reason) {
            const text = reason instanceof Error ? reason.message : "unexpected error";
            outcome = this.outcome(key, title, "failed", text, "plugin_error", null);
        }
        const { [key]: _done, ...stillPending } = this.state.pending;
        this.update({ pending: stillPending, outcomes: { ...this.state.outcomes, [key]: outcome } });
        this.poll();
        return outcome;
    }
    outcome(key, title, kind, text, code, record) {
        return { key, title, kind, text, code, record, at: Date.now() };
    }
    clearOutcome(key) {
        if (!(key in this.state.outcomes))
            return;
        const { [key]: _gone, ...rest } = this.state.outcomes;
        this.update({ outcomes: rest });
    }
    /**
     * Stop everything (plugin unload). Components that are still mounted give their holds back
     * later, when they unmount; those are void from here on, so the count cannot go below zero
     * and a later acquire() starts polling as it should.
     */
    shutdown() {
        this.generation += 1;
        if (this.timer !== null)
            clearInterval(this.timer);
        this.timer = null;
        this.holders = 0;
        this.pollAgain = false;
        this.listeners.clear();
    }
}
const store = new TvStore();
/**
 * Subscribe a component to the store. While `active`, the component also keeps the status
 * polling alive; polling stops when the last such component unmounts.
 *
 * While the store is `checking`, the component is given no status at all (as before the
 * first answer), so nothing out of date can be shown or acted on. A component that wants to
 * keep its layout through that moment passes `maskStale: false` and handles `checking` itself.
 */
function useTv(active = true, maskStale = true) {
    const [state, setState] = SP_REACT.useState(() => store.getState());
    SP_REACT.useEffect(() => {
        setState(store.getState());
        return store.subscribe(setState);
    }, []);
    SP_REACT.useEffect(() => (active ? store.acquire() : undefined), [active]);
    if (maskStale && state.checking)
        return { ...state, started: false, status: null, statusAsOf: 0 };
    return state;
}
/** Keep the status polling alive without re-rendering on every answer. */
function useHold() {
    SP_REACT.useEffect(() => store.acquire(), []);
}

// Every request the UI can make, in one place. Each function returns a promise that
// resolves to an Outcome (or a ConfigResult) and never rejects.
const ROUTE = "/tcl-me-remo";
const KEYS = {
    tvOn: "tv.on",
    tvOff: "tv.off",
    switchInput: "tv.input",
    wifiSwitch: "tv.wifi_input",
    testWake: "tv.test_wake",
    testSleep: "tv.test_sleep",
    macro: "tv.macro",
    connect: "bt.connect",
    disconnect: "bt.disconnect",
    forget: "bt.forget",
    install: "svc.install",
    restart: "svc.restart",
    remove: "svc.remove",
    wifiDiscover: "wifi.discover",
    wifiUnpair: "wifi.unpair",
    wifiTest: "wifi.test",
    wifiPairCancel: "wifi.pair_cancel",
};
// ---------------------------------------------------------------------- notifications
function toast(title, body, failed) {
    try {
        toaster.toast({ title, body, critical: failed, duration: failed ? 8000 : 3000 });
    }
    catch (reason) {
        console.warn("tcl-me-remo: could not show a notification", reason);
    }
}
/** Show an action's outcome as a notification (used by the Quick Access panel). */
function toastOutcome(outcome) {
    if (outcome.code === ALREADY_RUNNING)
        return;
    toast(`${outcome.title}: ${kindWord[outcome.kind]}`, outcome.text, outcome.kind === "failed");
}
// ---------------------------------------------------------------------- settings
/** Change settings; a rejected change is taken back and its reason shown. */
function applyConfig(patch) {
    return store.setConfig(patch).then((result) => {
        if (!result.ok)
            toast("Setting not changed", result.message, true);
        return result;
    });
}
// ---------------------------------------------------------------------- TV actions
function tvOn() {
    return store.run(KEYS.tvOn, "TV on", () => rpc("tv_on"));
}
function tvOff() {
    return store.run(KEYS.tvOff, "TV off", () => rpc("tv_off"));
}
function switchInput() {
    return store.run(KEYS.switchInput, "Switch input", () => rpc("switch_input"));
}
/**
 * Ask for the input over Wi-Fi only, whatever input.method is and with no fallback, so the
 * Wi-Fi method can be tried before it is turned on.
 */
function switchInputOverWifi() {
    return store.run(KEYS.wifiSwitch, "Switch input over Wi-Fi", () => rpc("switch_input", { via: "wifi" }));
}
/** Run exactly what a real wake or suspend would run, without the event having happened. */
function testEvent(name) {
    const wake = name === "resume";
    return store.run(wake ? KEYS.testWake : KEYS.testSleep, wake ? "Test wake" : "Test sleep", () => rpc("event", { name, simulate: true }));
}
function runMacro(key, macro) {
    return store.run(key, "Macro", () => rpc("run_macro", { macro }));
}
// ---------------------------------------------------------------------- Bluetooth
function connectTv() {
    return store.run(KEYS.connect, "Connect", () => rpc("connect"), (result) => result.ready
        ? { text: "The TV is connected" }
        : { kind: "failed", text: result.error || "The TV did not connect. Is it on, or in screenless standby?" });
}
function disconnectTv() {
    return store.run(KEYS.disconnect, "Disconnect", () => rpc("disconnect"), () => ({
        text: "Disconnected",
    }));
}
async function forgetTv() {
    const outcome = await store.run(KEYS.forget, "Forget TV", () => rpc("forget_tv"), () => ({
        text: "Forgotten here. Also remove this remote on the TV, under Remotes & Accessories.",
    }));
    await store.loadConfig();
    return outcome;
}
// ---------------------------------------------------------------------- the service itself
async function afterServiceChange(outcome) {
    await store.refreshService();
    store.poll();
    void store.loadConfig();
    return outcome;
}
function installService() {
    return store
        .run(KEYS.install, "Install service", installService$1, (result) => ({
        text: (result?.log ?? []).join("; ") || "The service is installed",
    }))
        .then(afterServiceChange);
}
function restartService() {
    return store
        .run(KEYS.restart, "Restart service", restartService$1, () => ({ text: "The service was restarted" }))
        .then(afterServiceChange);
}
/**
 * What a removal is followed by. The page then shows "not installed" with an Install button,
 * which is not the way on for somebody who wants to be rid of it.
 */
const AFTER_REMOVE = "Now uninstall the plugin in Decky (Decky settings > Plugins): otherwise it installs the service again the next time it loads.";
function removeService(purge) {
    return store
        .run(KEYS.remove, "Remove service", () => removeService$1(purge), (result) => ({
        text: `${(result?.log ?? []).join("; ") || "The service was removed"}. ${AFTER_REMOVE}`,
    }))
        .then(afterServiceChange);
}
// ---------------------------------------------------------------------- Wi-Fi assist
async function wifiDiscover() {
    let devices = [];
    const outcome = await store.run(KEYS.wifiDiscover, "Search", () => rpc("wifi_discover"), (result) => {
        devices = Array.isArray(result?.devices) ? result.devices : [];
        return {
            text: devices.length === 1 ? "Found 1 TV" : `Found ${devices.length} TVs`,
            kind: devices.length ? "ok" : "skipped",
        };
    });
    return { outcome, devices };
}
async function wifiUnpair() {
    const outcome = await store.run(KEYS.wifiUnpair, "Unpair Wi-Fi", () => rpc("wifi_unpair"), () => ({
        text: "The Wi-Fi pairing was removed and the assist turned off",
    }));
    await store.loadConfig();
    return outcome;
}
/** Close a Wi-Fi pairing that was left open (the TV is still showing its code). */
function wifiPairCancel() {
    return store.run(KEYS.wifiPairCancel, "Cancel pairing", () => rpc("wifi_pair_cancel"), () => ({
        text: "The pairing was cancelled",
    }));
}
function wifiTest() {
    return store.run(KEYS.wifiTest, "Check connection", () => rpc("wifi_test"), (result) => {
        const name = result.name ? `${result.name} answers` : "The TV answers";
        const power = result.on === true ? "; it is on" : result.on === false ? "; it is in standby" : "";
        return { text: `${name} over Wi-Fi${power}` };
    });
}

function serviceVerdict(state) {
    const info = state.service;
    const reason = state.statusError?.message ?? "";
    if (!info) {
        if (state.serviceError) {
            return {
                title: "The background service does not answer",
                lines: [reason, `Its state could not be checked: ${state.serviceError}`].filter(Boolean),
                fix: "Repair service",
            };
        }
        return {
            title: "The background service does not answer",
            lines: [reason, "Checking what is installed."].filter(Boolean),
            fix: "",
        };
    }
    if (info.installing) {
        return {
            title: "Installing the background service",
            lines: ["The plugin installs or updates it when it loads. This takes a moment.", ...info.problems],
            fix: "",
            calm: true,
        };
    }
    if (!info.installed) {
        return {
            title: "The background service is not installed",
            lines: [
                "The service does the work: it keeps the Bluetooth link to the TV and reacts when SteamOS sleeps and wakes.",
                // The plugin backend installs it in the background when the plugin loads and does not
                // say so while that runs; without a reported problem this may be that moment.
                ...(info.problems.length
                    ? info.problems
                    : ["The plugin installs it when it loads, which takes a moment. If this message stays, choose Install service."]),
            ],
            fix: "Install service",
        };
    }
    if (!info.active) {
        return {
            title: "The background service is not running",
            lines: info.problems.length ? info.problems : ["It is installed but stopped."],
            fix: "Repair service",
        };
    }
    return {
        title: "The background service is running but does not answer",
        lines: [reason, ...info.problems].filter(Boolean),
        fix: "Repair service",
    };
}
/** The settings-page version; the Quick Access panel renders the same verdict its own way. */
const ServiceProblem = ({ state }) => {
    const verdict = serviceVerdict(state);
    const working = KEYS.install in state.pending;
    const outcome = state.outcomes[KEYS.install];
    return (SP_JSX.jsx("div", { style: {
            background: verdict.calm ? "rgba(255, 255, 255, 0.06)" : colors.badBackground,
            borderLeft: `4px solid ${verdict.calm ? colors.dim : colors.bad}`,
            marginBottom: "12px",
        }, children: SP_JSX.jsx(DFL.Field, { focusable: !verdict.fix, bottomSeparator: "none", icon: verdict.calm ? SP_JSX.jsx(DFL.Spinner, { style: { width: "18px", height: "18px" } }) : SP_JSX.jsx(FaExclamationTriangle, { style: { color: colors.bad } }), label: SP_JSX.jsx("span", { style: { color: verdict.calm ? colors.text : colors.bad }, children: verdict.title }), description: SP_JSX.jsxs("div", { style: { color: colors.text }, children: [verdict.lines.map((line, index) => (SP_JSX.jsx("div", { style: { overflowWrap: "anywhere" }, children: line }, index))), outcome && !working && (SP_JSX.jsxs("div", { style: { color: kindColor[outcome.kind], marginTop: "4px", overflowWrap: "anywhere" }, children: [outcome.title, ": ", kindWord[outcome.kind], ". ", outcome.text] }))] }), children: verdict.fix && (SP_JSX.jsx(DFL.DialogButton, { style: { width: "auto", minWidth: "10em", padding: "10px 16px" }, disabled: working, onClick: () => void installService(), children: working ? "Working…" : verdict.fix })) }) }));
};

const noop = () => undefined;
const Section = ({ title, children }) => (SP_JSX.jsxs(DFL.DialogControlsSection, { children: [SP_JSX.jsx(DFL.DialogControlsSectionHeader, { children: title }), children] }));
/**
 * A row of buttons. Steam's gamepad navigation only moves sideways between siblings of a
 * Focusable that says so, hence the wrapper.
 */
const ButtonRow = ({ children, style }) => (SP_JSX.jsx(DFL.Focusable, { "flow-children": "right", style: { display: "flex", flexWrap: "wrap", gap: "8px", ...style }, children: children }));
const rowButton = { width: "auto", minWidth: "7em", flex: "0 0 auto", padding: "10px 16px" };
/** Read-only text the D-pad can stop on, so it can be scrolled into view. */
const Note = ({ label, children, icon }) => (SP_JSX.jsx(DFL.Field, { focusable: true, label: label, description: children, icon: icon, bottomSeparator: "none" }));
/** One line of a checklist: a tick when `done`, an empty circle when not, nothing when unknowable. */
const CheckIcon = ({ done }) => done === null ? (SP_JSX.jsx(FaRegCircle, { style: { color: colors.dim, opacity: 0.4 } })) : done ? (SP_JSX.jsx(FaCheckCircle, { style: { color: colors.ok } })) : (SP_JSX.jsx(FaRegCircle, { style: { color: colors.warn } }));
const Value = ({ children, color }) => (SP_JSX.jsx("span", { style: { color: color ?? colors.text, textAlign: "right", overflowWrap: "anywhere" }, children: children }));
const Steps = ({ steps }) => steps.length ? (SP_JSX.jsx("ol", { style: { margin: "6px 0 0 0", paddingLeft: "1.4em", color: colors.text }, children: steps.map((step, index) => (SP_JSX.jsx("li", { style: { margin: "2px 0", overflowWrap: "anywhere" }, children: step }, index))) })) : (SP_JSX.jsx("div", { style: { marginTop: "6px", color: colors.dim }, children: "No steps were recorded." }));
/** An action record: what, why, how it ended; the step trace opens with one press. */
const RecordView = ({ record, title, startOpen, }) => {
    const kind = recordKind(record);
    const [open, setOpen] = SP_REACT.useState(startOpen ?? kind === "failed");
    const heading = `${title ?? actionName(record)}: ${kindWord[kind]}`;
    return (SP_JSX.jsx(DFL.Field, { label: SP_JSX.jsxs("span", { style: { color: kindColor[kind] }, children: [kind === "failed" && SP_JSX.jsx(FaExclamationTriangle, { style: { marginRight: "8px", verticalAlign: "-2px" } }), heading] }), description: SP_JSX.jsxs("div", { children: [SP_JSX.jsx("div", { style: { color: colors.text, overflowWrap: "anywhere" }, children: record.detail || "No detail was given." }), SP_JSX.jsxs("div", { style: { color: colors.dim }, children: [triggerName(record), " \u00B7 ", clockTime(record.time), " \u00B7 took ", seconds(record.seconds)] }), open && SP_JSX.jsx(Steps, { steps: record.steps })] }), bottomSeparator: "none", children: SP_JSX.jsx(DFL.DialogButton, { style: rowButton, onClick: () => setOpen(!open), children: open ? "Hide steps" : `Show steps (${record.steps.length})` }) }));
};
/**
 * How the action behind `actionKey` went, if it has been run.
 *
 * A result describes one moment, so it does not stay for ever: it goes when the user leaves
 * the page after having seen it, and when the live state has since come to say otherwise
 * (`contradicted`: "Forget TV: done" next to a TV that has been paired again). What stays
 * says how old it is.
 */
const OutcomeView = ({ state, actionKey, contradicted, }) => {
    const title = state.pending[actionKey];
    const outcome = state.outcomes[actionKey];
    const seen = SP_REACT.useRef(false);
    if (outcome)
        seen.current = true;
    // Only a status that was asked for after the action ended can contradict its result.
    const outdated = !!outcome && contradicted === true && state.statusAsOf > outcome.at;
    SP_REACT.useEffect(() => {
        if (outdated)
            store.clearOutcome(actionKey);
    }, [outdated, actionKey]);
    SP_REACT.useEffect(() => () => {
        if (seen.current)
            store.clearOutcome(actionKey);
    }, [actionKey]);
    if (title !== undefined) {
        return (SP_JSX.jsx(DFL.Field, { focusable: true, bottomSeparator: "none", label: SP_JSX.jsxs("span", { children: [SP_JSX.jsx(DFL.Spinner, { style: { width: "16px", height: "16px", marginRight: "8px", verticalAlign: "-3px" } }), title, ": working"] }), description: "This can take a while when the TV has to be reached first." }));
    }
    if (!outcome || outdated)
        return null;
    if (outcome.record)
        return SP_JSX.jsx(RecordView, { record: outcome.record, title: outcome.title });
    return (SP_JSX.jsx(DFL.Field, { focusable: true, bottomSeparator: "none", onActivate: outcome.kind === "failed" ? noop : undefined, label: SP_JSX.jsxs("span", { style: { color: kindColor[outcome.kind] }, children: [outcome.kind === "failed" && SP_JSX.jsx(FaExclamationTriangle, { style: { marginRight: "8px", verticalAlign: "-2px" } }), outcome.title, ": ", kindWord[outcome.kind]] }), description: SP_JSX.jsxs("div", { children: [SP_JSX.jsx("div", { style: { color: colors.text, overflowWrap: "anywhere" }, children: outcome.text }), SP_JSX.jsx("div", { style: { color: colors.dim }, children: timeAgo(outcome.at / 1000) })] }) }));
};
/** A button that is disabled while its own request is in flight and while the service is busy. */
const ActionButton = ({ state, actionKey, onClick, disabled, children, style }) => (SP_JSX.jsx(DFL.DialogButton, { style: { ...rowButton, ...style }, disabled: disabled || actionDisabled(state, actionKey), onClick: () => onClick(), children: actionKey in state.pending ? "Working…" : children }));
const Banner = ({ title, text, onDismiss }) => (SP_JSX.jsx("div", { style: { background: colors.badBackground, borderLeft: `4px solid ${colors.bad}`, marginBottom: "12px" }, children: SP_JSX.jsx(DFL.Field, { focusable: !onDismiss, bottomSeparator: "none", icon: SP_JSX.jsx(FaExclamationTriangle, { style: { color: colors.bad } }), label: SP_JSX.jsx("span", { style: { color: colors.bad }, children: title }), description: SP_JSX.jsx("span", { style: { color: colors.text, overflowWrap: "anywhere" }, children: text }), children: onDismiss && (SP_JSX.jsx(DFL.DialogButton, { style: rowButton, onClick: () => onDismiss(), children: "Dismiss" })) }) }));
/**
 * The frame of every settings page: problems first (service unreachable, a rejected
 * setting, settings that did not load), then the page.
 */
const Page = ({ state, children, worksOffline = false, }) => {
    const offline = state.started && !state.status;
    // Without the service there is nothing true to show: no stale status, no settings that
    // cannot be changed. Pages that are about the service itself say so with worksOffline.
    const ready = worksOffline ? state.started : !!state.status && !!state.config;
    return (SP_JSX.jsxs(DFL.DialogBody, { children: [offline && SP_JSX.jsx(ServiceProblem, { state: state }), state.configError && (SP_JSX.jsx(Banner, { title: "That change was not accepted", text: state.configError, onDismiss: () => store.clearConfigError() })), state.status && !state.config && state.configLoadError && (SP_JSX.jsx(Banner, { title: "The settings could not be loaded", text: state.configLoadError })), !state.started && SP_JSX.jsx(Note, { label: "Checking the background service", children: "One moment." }), state.status && !state.config && !state.configLoadError && SP_JSX.jsx(Note, { label: "Loading the settings", children: "One moment." }), ready ? children : null] }));
};

const footerButton = { width: "auto", minWidth: "9em", flex: "0 0 auto" };
const Footer = ({ children }) => (SP_JSX.jsx(DFL.DialogFooter, { children: SP_JSX.jsx(DFL.Focusable, { "flow-children": "right", style: { display: "flex", gap: "10px", justifyContent: "flex-end" }, children: children }) }));
const paragraph$1 = { margin: "0 0 10px 0" };
// ---------------------------------------------------------------------- an action's trace
const RecordModal = ({ closeModal, record }) => {
    const kind = recordKind(record);
    return (SP_JSX.jsxs(DFL.ConfirmModal, { strTitle: SP_JSX.jsx("span", { style: { color: kindColor[kind] }, children: `${actionName(record)}: ${kindWord[kind]}` }), strOKButtonText: "Close", bAlertDialog: true, closeModal: closeModal, children: [SP_JSX.jsx("div", { style: { overflowWrap: "anywhere" }, children: record.detail || "No detail was given." }), SP_JSX.jsxs("div", { style: { color: colors.dim }, children: [triggerName(record), " \u00B7 ", clockTime(record.time), " \u00B7 took ", seconds(record.seconds)] }), SP_JSX.jsx(Steps, { steps: record.steps })] }));
};
function minutes(secondsLeft) {
    const whole = Math.max(0, Math.round(secondsLeft));
    return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}
/**
 * Opens pairing mode, tells the user what to do on the TV, and follows the service's status
 * until a TV is paired or pairing mode closes. Closing the dialog closes pairing mode.
 */
const PairModal = ({ closeModal }) => {
    const state = useTv();
    const [phase, setPhase] = SP_REACT.useState("starting");
    const [message, setMessage] = SP_REACT.useState("");
    const phaseNow = SP_REACT.useRef(phase);
    phaseNow.current = phase;
    const mounted = SP_REACT.useRef(true);
    const openedAt = SP_REACT.useRef(0);
    const sawOpen = SP_REACT.useRef(false);
    const pairedBefore = SP_REACT.useRef(false);
    const start = () => {
        setPhase("starting");
        setMessage("");
        sawOpen.current = false;
        pairedBefore.current = store.getState().status?.bluetooth?.paired === true;
        void rpc("pair_start").then((reply) => {
            if (!mounted.current) {
                // Closed while the request was on its way: do not leave pairing mode open.
                if (reply.ok)
                    void rpc("pair_cancel");
                return;
            }
            if (!reply.ok) {
                setMessage(reply.error.message);
                setPhase("failed");
                return;
            }
            openedAt.current = Date.now();
            setPhase("open");
            store.poll();
        });
    };
    SP_REACT.useEffect(() => {
        mounted.current = true;
        start();
        return () => {
            mounted.current = false;
            if (phaseNow.current === "open")
                void rpc("pair_cancel").then(() => store.poll());
        };
    }, []);
    const bluetooth = state.status?.bluetooth;
    SP_REACT.useEffect(() => {
        if (phase !== "open" || !bluetooth)
            return;
        if (bluetooth.pairing) {
            sawOpen.current = true;
            return;
        }
        // A status that was requested before pairing mode opened may still arrive; only trust
        // "not pairing" once pairing mode has been seen open, or after a moment.
        const settled = sawOpen.current || Date.now() - openedAt.current > 2500;
        if (bluetooth.paired && (settled || !pairedBefore.current)) {
            setPhase("paired");
            void store.loadConfig();
        }
        else if (!bluetooth.paired && settled) {
            setMessage(bluetooth.error || "Pairing mode closed before a TV paired.");
            setPhase("closed");
        }
    }, [phase, bluetooth?.pairing, bluetooth?.paired, state.status?.uptime]);
    const close = () => closeModal?.();
    const name = state.config?.bluetooth.device_name || "SteamOS Remote";
    return (SP_JSX.jsxs(DFL.ModalRoot, { closeModal: close, onCancel: close, children: [SP_JSX.jsx(DFL.DialogHeader, { children: "Pair TV" }), SP_JSX.jsxs(DFL.DialogBodyText, { children: [phase === "starting" && SP_JSX.jsx("p", { style: paragraph$1, children: "Opening pairing mode." }), phase === "open" && (SP_JSX.jsxs("div", { children: [SP_JSX.jsxs("p", { style: paragraph$1, children: ["Pairing mode is open", bluetooth?.pairing ? ` for another ${minutes(bluetooth.pairing_seconds_left)}` : "", ". On the TV:"] }), SP_JSX.jsxs("ol", { style: { margin: "0 0 10px 0", paddingLeft: "1.4em" }, children: [SP_JSX.jsx("li", { children: "Open Settings > Remotes & Accessories > Pair accessory (older firmware: Add accessory)." }), SP_JSX.jsxs("li", { children: ["Pick ", SP_JSX.jsx("b", { children: name }), " in the list and confirm."] })] }), bluetooth?.pairing && bluetooth.error && (
                            // What the service has to say about the open pairing mode matters now, not after
                            // it has timed out: for one, that another Bluetooth pairing screen is in the way.
                            SP_JSX.jsxs("p", { style: { ...paragraph$1, color: colors.warn, overflowWrap: "anywhere" }, children: ["Bluetooth problem: ", bluetooth.error] })), SP_JSX.jsx("p", { style: { ...paragraph$1, color: colors.dim }, children: "If this machine does not show up: check that Bluetooth is on in SteamOS, move closer, and on the TV remove any earlier pairing with this machine first." }), !state.status && state.started && (SP_JSX.jsx("p", { style: { ...paragraph$1, color: colors.bad }, children: "The background service stopped answering." }))] })), phase === "paired" && (SP_JSX.jsxs("div", { children: [SP_JSX.jsxs("p", { style: { ...paragraph$1, color: colors.ok }, children: ["Paired with ", bluetooth?.tv_name || "the TV", "."] }), SP_JSX.jsx("p", { style: paragraph$1, children: "Pair once; the pairing survives restarts of both sides. Next, check that it works: turn the TV off and on again from the panel." })] })), (phase === "closed" || phase === "failed") && (SP_JSX.jsxs("div", { children: [SP_JSX.jsx("p", { style: { ...paragraph$1, color: colors.bad }, children: phase === "failed" ? "Pairing mode could not be opened." : "No TV was paired." }), SP_JSX.jsx("p", { style: { ...paragraph$1, overflowWrap: "anywhere" }, children: message })] }))] }), SP_JSX.jsxs(Footer, { children: [(phase === "closed" || phase === "failed") && (SP_JSX.jsx(DFL.DialogButton, { style: footerButton, onClick: () => start(), children: "Try again" })), SP_JSX.jsx(DFL.DialogButton, { style: footerButton, onClick: close, children: phase === "paired" ? "Done" : phase === "starting" || phase === "open" ? "Cancel pairing" : "Close" })] })] }));
};
// ---------------------------------------------------------------------- confirmations
const ForgetModal = ({ closeModal, tvName, remoteName, }) => (SP_JSX.jsxs(DFL.ConfirmModal, { strTitle: `Forget ${tvName || "the TV"}?`, strOKButtonText: "Forget TV", strCancelButtonText: "Keep", bDestructiveWarning: true, onOK: () => void forgetTv(), closeModal: closeModal, children: [SP_JSX.jsx("p", { style: paragraph$1, children: "This removes the pairing on this machine. The TV is no longer controlled." }), SP_JSX.jsxs("p", { style: paragraph$1, children: ["Also remove ", SP_JSX.jsx("b", { children: remoteName || "SteamOS Remote" }), " on the TV, under Settings > Remotes & Accessories. Both sides have to forget before they can be paired again."] })] }));
const RemoveServiceModal = ({ closeModal }) => {
    const [purge, setPurge] = SP_REACT.useState(false);
    return (SP_JSX.jsxs(DFL.ConfirmModal, { strTitle: "Remove the background service?", strOKButtonText: purge ? "Remove service and settings" : "Remove service", strCancelButtonText: "Keep", bDestructiveWarning: true, onOK: () => void removeService(purge), closeModal: closeModal, children: [SP_JSX.jsx("p", { style: paragraph$1, children: "The service is stopped and removed. The TV is no longer turned on or off with this machine. The plugin installs the service again the next time it loads; to remove it for good, uninstall the plugin in Decky." }), SP_JSX.jsx(DFL.ToggleField, { label: "Also delete settings and the pairing", description: "Deletes the settings and what was learned about the TV, and removes the Bluetooth pairing with the TV from this machine: the TV has to be paired again afterwards. On the TV, remove this remote under Remotes & Accessories as well.", checked: purge, onChange: setPurge })] }));
};
/** Shown before input switching is turned on: what will be sent to the TV, and when. */
const ConfirmSendModal = ({ closeModal, title, okLabel, blocked, waiting = false, children, onOK }) => (SP_JSX.jsxs(DFL.ConfirmModal, { strTitle: title, strOKButtonText: okLabel, strCancelButtonText: "Cancel", bOKDisabled: blocked !== "" || waiting, onOK: () => {
        if (!blocked && !waiting)
            onOK();
    }, closeModal: closeModal, children: [children, blocked && !waiting && SP_JSX.jsx("p", { style: { ...paragraph$1, color: colors.bad }, children: blocked })] }));
/**
 * Pairs the Wi-Fi assist: asks the TV to show a code, takes the code, finishes. Closing the
 * dialog before the end cancels the pairing on the service.
 */
const WifiPairModal = ({ closeModal, host, name, }) => {
    const [phase, setPhase] = SP_REACT.useState("starting");
    const [message, setMessage] = SP_REACT.useState("");
    const [code, setCode] = SP_REACT.useState("");
    const phaseNow = SP_REACT.useRef(phase);
    phaseNow.current = phase;
    const mounted = SP_REACT.useRef(true);
    const start = () => {
        setPhase("starting");
        setMessage("");
        setCode("");
        void rpc("wifi_pair_start", { host }).then((reply) => {
            if (!mounted.current) {
                if (reply.ok)
                    void rpc("wifi_pair_cancel");
                return;
            }
            if (reply.ok) {
                setPhase("code");
            }
            else {
                setMessage(reply.error.message);
                setPhase("failed");
            }
        });
    };
    const finish = () => {
        const entered = code.trim();
        if (!entered || phaseNow.current !== "code")
            return;
        setPhase("finishing");
        const params = name ? { code: entered, name } : { code: entered };
        void rpc("wifi_pair_finish", params).then((reply) => {
            if (reply.ok)
                void store.loadConfig();
            if (!mounted.current) {
                // Closed while the code was being checked. A code the TV turned down leaves the
                // pairing open on the service (it may be tried again), so close it here.
                if (reply.ok)
                    store.poll();
                else
                    void rpc("wifi_pair_cancel").then(() => store.poll());
                return;
            }
            store.poll();
            if (reply.ok) {
                setPhase("done");
            }
            else {
                setMessage(reply.error.message);
                setPhase("failed");
            }
        });
    };
    SP_REACT.useEffect(() => {
        mounted.current = true;
        start();
        return () => {
            mounted.current = false;
            const now = phaseNow.current;
            // "failed" included: the service may still hold a half-open pairing with the TV.
            if (now === "code" || now === "failed")
                void rpc("wifi_pair_cancel").then(() => store.poll());
        };
    }, []);
    const close = () => closeModal?.();
    const tv = name || host;
    return (SP_JSX.jsxs(DFL.ModalRoot, { closeModal: close, onCancel: close, children: [SP_JSX.jsx(DFL.DialogHeader, { children: "Pair over Wi-Fi" }), SP_JSX.jsxs(DFL.DialogBodyText, { children: [phase === "starting" && SP_JSX.jsxs("p", { style: paragraph$1, children: ["Asking ", tv, " to show a pairing code."] }), (phase === "code" || phase === "finishing") && (SP_JSX.jsxs("div", { children: [SP_JSX.jsx("p", { style: paragraph$1, children: "The TV shows a six-character code. Enter it here." }), SP_JSX.jsx(DFL.TextField, { label: "Code on the TV", value: code, focusOnMount: true, bShowClearAction: true, disabled: phase === "finishing", onChange: (event) => setCode(event.target.value) })] })), phase === "done" && (SP_JSX.jsxs("div", { children: [SP_JSX.jsxs("p", { style: { ...paragraph$1, color: colors.ok }, children: ["Paired with ", tv, " over Wi-Fi."] }), SP_JSX.jsx("p", { style: paragraph$1, children: "The Wi-Fi assist is now turned on." })] })), phase === "failed" && (SP_JSX.jsxs("div", { children: [SP_JSX.jsx("p", { style: { ...paragraph$1, color: colors.bad }, children: "Pairing over Wi-Fi did not work." }), SP_JSX.jsx("p", { style: { ...paragraph$1, overflowWrap: "anywhere" }, children: message })] }))] }), SP_JSX.jsxs(Footer, { children: [(phase === "code" || phase === "finishing") && (SP_JSX.jsx(DFL.DialogButton, { style: footerButton, disabled: phase === "finishing" || !code.trim(), onClick: () => finish(), children: phase === "finishing" ? "Pairing…" : "Finish pairing" })), phase === "failed" && (SP_JSX.jsx(DFL.DialogButton, { style: footerButton, onClick: () => start(), children: "Start again" })), SP_JSX.jsx(DFL.DialogButton, { style: footerButton, onClick: close, children: phase === "done" ? "Done" : phase === "failed" ? "Close" : "Cancel" })] })] }));
};

/** Open a page of the full-screen settings; the Quick Access menu closes after navigating. */
function openSettings(page) {
    try {
        DFL.Navigation.Navigate(`${ROUTE}/${page}`);
        DFL.Navigation.CloseSideMenus();
    }
    catch (reason) {
        console.warn("tcl-me-remo: could not open the settings page", reason);
    }
}
const TitleView = () => (SP_JSX.jsxs(DFL.Focusable, { style: {
        display: "flex",
        padding: "0",
        width: "100%",
        boxShadow: "none",
        alignItems: "center",
        justifyContent: "space-between",
    }, className: DFL.staticClasses.Title, children: [SP_JSX.jsx("div", { children: "TV remote" }), SP_JSX.jsx(DFL.DialogButton, { style: { height: "28px", width: "40px", minWidth: 0, padding: "10px 12px" }, onClick: () => openSettings("setup"), children: SP_JSX.jsx(FaCog, { style: { marginTop: "-4px", display: "block" } }) })] }));

const Dot = ({ color }) => (SP_JSX.jsx("span", { style: {
        display: "inline-block",
        width: "8px",
        height: "8px",
        borderRadius: "50%",
        background: color,
        marginRight: "8px",
        flex: "0 0 auto",
    } }));
const Line = ({ color, children }) => (SP_JSX.jsxs("div", { style: { display: "flex", alignItems: "center", margin: "3px 0", color: colors.text }, children: [SP_JSX.jsx(Dot, { color: color }), SP_JSX.jsx("span", { style: { overflowWrap: "anywhere" }, children: children })] }));
/** A failure nobody can overlook: red, with the reason in the service's words. */
const Alarm = ({ title, children }) => (SP_JSX.jsxs("div", { style: {
        background: colors.badBackground,
        borderLeft: `4px solid ${colors.bad}`,
        padding: "8px 10px",
        margin: "4px 0",
        color: colors.text,
    }, children: [SP_JSX.jsxs("div", { style: { color: colors.bad, fontWeight: "bold", display: "flex", alignItems: "center" }, children: [SP_JSX.jsx(FaExclamationTriangle, { style: { marginRight: "8px", flex: "0 0 auto" } }), title] }), SP_JSX.jsx("div", { style: { overflowWrap: "anywhere", marginTop: "2px" }, children: children })] }));
function run(action) {
    void action().then(toastOutcome, (reason) => console.warn("tcl-me-remo: action failed", reason));
}
function tvLine(state) {
    const bluetooth = state.status?.bluetooth;
    if (!bluetooth)
        return { color: colors.dim, text: "TV state unknown" };
    if (!bluetooth.available)
        return { color: colors.bad, text: bluetooth.error || "Bluetooth is not available" };
    if (!bluetooth.powered)
        return { color: colors.bad, text: "Bluetooth is turned off in SteamOS" };
    if (bluetooth.pairing)
        return { color: colors.warn, text: "Pairing mode is open" };
    if (!bluetooth.paired)
        return { color: colors.warn, text: "No TV paired" };
    const name = bluetooth.tv_name || "TV";
    if (bluetooth.ready)
        return { color: colors.ok, text: `${name}: connected` };
    if (bluetooth.connected)
        return { color: colors.warn, text: `${name}: connecting` };
    return { color: colors.dim, text: `${name}: paired, not connected` };
}
// A refusal is news for this long; after that it is history (and it was announced when it happened).
const REFUSAL_SHOWN_MS = 60000;
/**
 * A request that was refused outright (busy, not paired, ...) leaves no action record. It is
 * shown until something newer has happened: an action has finished since, the service is no
 * longer busy with what it was refused for, or a minute has passed.
 */
function refusedOutcome(state, now = Date.now()) {
    let latest = null;
    for (const key of [KEYS.tvOn, KEYS.tvOff, KEYS.switchInput, KEYS.install]) {
        const outcome = state.outcomes[key];
        if (outcome && outcome.kind === "failed" && !outcome.record && (!latest || outcome.at > latest.at))
            latest = outcome;
    }
    if (!latest || now - latest.at > REFUSAL_SHOWN_MS)
        return null;
    const status = state.status;
    const last = status?.last_action;
    if (last && (last.time + last.seconds) * 1000 > latest.at)
        return null;
    if (latest.code === "busy" && status && !status.busy && state.statusAsOf > latest.at)
        return null;
    return latest;
}
const StatusBlock = ({ state }) => {
    const status = state.status;
    if (!status)
        return null;
    if (state.checking) {
        // The panel was reopened (or the service is slow to answer): what is known is from
        // before, so say nothing about the TV until the service has answered.
        return (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.Focusable, { style: { padding: "2px 0" }, onActivate: () => undefined, children: SP_JSX.jsx(Line, { color: colors.dim, children: "Checking the background service" }) }) }));
    }
    const busy = busyReason(state);
    const tv = tvLine(state);
    const last = status.last_action;
    const lastKind = last ? recordKind(last) : null;
    const refused = refusedOutcome(state);
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.Focusable, { style: { padding: "2px 0" }, onActivate: () => {
                        if (last)
                            DFL.showModal(SP_JSX.jsx(RecordModal, { record: last }));
                    }, children: SP_JSX.jsxs("div", { style: { width: "100%" }, children: [SP_JSX.jsx(Line, { color: status.phase === "running" ? colors.ok : colors.warn, children: status.phase === "running" ? "Service running" : "SteamOS is going to sleep or shutting down" }), SP_JSX.jsx(Line, { color: tv.color, children: tv.text }), busy ? (SP_JSX.jsxs("div", { style: { display: "flex", alignItems: "center", margin: "3px 0", color: colors.text }, children: [SP_JSX.jsx(DFL.Spinner, { style: { width: "14px", height: "14px", marginRight: "6px", marginLeft: "-3px" } }), SP_JSX.jsxs("span", { style: { overflowWrap: "anywhere" }, children: ["Working: ", busy] })] })) : (last &&
                                lastKind !== "failed" && (SP_JSX.jsxs(Line, { color: kindColor[lastKind ?? "ok"], children: [actionName(last), ": ", kindWord[lastKind ?? "ok"], " (", triggerName(last), ", ", timeAgo(last.time), ")", lastKind === "skipped" ? `. ${last.detail}` : ""] }))), !status.enabled && SP_JSX.jsx(Line, { color: colors.warn, children: "Automatic actions are off" }), status.config_problems.length > 0 && SP_JSX.jsx(Line, { color: colors.warn, children: "Settings problem: see Diagnostics" })] }) }) }), refused && refused.code === "busy" && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs(Line, { color: colors.warn, children: [refused.title, " was not started. ", refused.text] }) })), refused && refused.code !== "busy" && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(Alarm, { title: `${refused.title} did not start`, children: refused.text }) })), !busy && last && lastKind === "failed" && (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs(Alarm, { title: `${actionName(last)} failed`, children: [last.detail, SP_JSX.jsxs("div", { style: { color: colors.dim }, children: [triggerName(last), ", ", timeAgo(last.time)] })] }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => DFL.showModal(SP_JSX.jsx(RecordModal, { record: last })), children: "Show what was tried" }) })] }))] }));
};
const ServiceDown = ({ state }) => {
    const verdict = serviceVerdict(state);
    const working = KEYS.install in state.pending;
    return (SP_JSX.jsxs(DFL.PanelSection, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: verdict.calm ? (SP_JSX.jsxs("div", { style: { color: colors.text, margin: "4px 0" }, children: [SP_JSX.jsxs("div", { style: { display: "flex", alignItems: "center", fontWeight: "bold" }, children: [SP_JSX.jsx(DFL.Spinner, { style: { width: "14px", height: "14px", marginRight: "8px", flex: "0 0 auto" } }), verdict.title] }), verdict.lines.map((line, index) => (SP_JSX.jsx("div", { style: { overflowWrap: "anywhere", marginTop: "2px" }, children: line }, index)))] })) : (SP_JSX.jsx(Alarm, { title: verdict.title, children: verdict.lines.map((line, index) => (SP_JSX.jsx("div", { children: line }, index))) })) }), verdict.fix && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: working, onClick: () => run(installService), children: working ? "Working…" : verdict.fix }) })), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => openSettings("diagnostics"), children: "Diagnostics" }) })] }));
};
const PowerButtons = ({ state }) => {
    const status = state.status;
    if (!status)
        return null;
    const bluetooth = status.bluetooth;
    if (!bluetooth.paired) {
        return (SP_JSX.jsxs(DFL.PanelSection, { title: "Get started", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { color: colors.text, margin: "2px 0 6px 0" }, children: "Pair this machine with the TV once, as a Bluetooth remote. Setup lists the TV settings to change first." }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: !bluetooth.available || state.checking, onClick: () => DFL.showModal(SP_JSX.jsx(PairModal, {})), children: "Pair TV" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => openSettings("setup"), children: "Setup" }) })] }));
    }
    const label = (key, text) => (key in state.pending ? "Working…" : text);
    return (SP_JSX.jsxs(DFL.PanelSection, { title: "TV", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: actionDisabled(state, KEYS.tvOn), onClick: () => run(tvOn), children: label(KEYS.tvOn, "TV on") }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: actionDisabled(state, KEYS.tvOff), onClick: () => run(tvOff), children: label(KEYS.tvOff, "TV off") }) }), status.input.method !== "none" && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: actionDisabled(state, KEYS.switchInput), onClick: () => run(switchInput), children: label(KEYS.switchInput, `Switch to HDMI ${status.input.hdmi_port}`) }) }))] }));
};
const Toggles = ({ state }) => {
    const config = state.config;
    if (!state.status || !config)
        return null;
    const events = config.events;
    const onBoth = events.boot_tv_on && events.resume_tv_on;
    const mixed = events.boot_tv_on !== events.resume_tv_on;
    return (SP_JSX.jsxs(DFL.PanelSection, { title: "Automatic", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ToggleField, { label: "Automatic actions", checked: config.enabled, onChange: (enabled) => void applyConfig({ enabled }) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ToggleField, { label: "TV on at start and wake", description: mixed ? "Start and wake are set differently; see Settings." : undefined, checked: onBoth, disabled: !config.enabled, onChange: (on) => void applyConfig({ events: { boot_tv_on: on, resume_tv_on: on } }) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ToggleField, { label: "TV off on suspend", checked: events.suspend_tv_off, disabled: !config.enabled, onChange: (on) => void applyConfig({ events: { suspend_tv_off: on } }) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ToggleField, { label: "TV off on shutdown", checked: events.shutdown_tv_off, disabled: !config.enabled, onChange: (on) => void applyConfig({ events: { shutdown_tv_off: on } }) }) })] }));
};
const QuickPanel = () => {
    const visible = useQuickAccessVisible();
    // The panel keeps its layout while a stale status is being replaced (the Quick Access Menu
    // was reopened): the buttons stay where they are, disabled, and the status lines give way
    // to "Checking". Swapping the whole panel for a moment would throw the gamepad focus.
    const state = useTv(visible, false);
    if (!state.started) {
        return (SP_JSX.jsxs(DFL.PanelSection, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(Line, { color: colors.dim, children: "Checking the background service" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => openSettings("diagnostics"), children: "Diagnostics" }) })] }));
    }
    if (!state.status)
        return SP_JSX.jsx(ServiceDown, { state: state });
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(StatusBlock, { state: state }) }), SP_JSX.jsx(PowerButtons, { state: state }), SP_JSX.jsx(Toggles, { state: state }), SP_JSX.jsxs(DFL.PanelSection, { children: [state.status.bluetooth.paired && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => openSettings("remote"), children: "Remote" }) })), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => openSettings("setup"), children: "Settings" }) })] })] }));
};

function isObject(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}
/** The schema entry of a setting, by dotted path ("input.hdmi_port"). */
function settingInfo(bundle, path) {
    let node = bundle?.schema;
    for (const part of path.split(".")) {
        if (!isObject(node))
            return null;
        node = node[part];
    }
    return isObject(node) && typeof node.type === "string" ? node : null;
}
function settingValue(config, path) {
    let node = config;
    for (const part of path.split(".")) {
        if (!isObject(node))
            return undefined;
        node = node[part];
    }
    return node;
}
function patchFor(path, value) {
    const parts = path.split(".");
    let patch = value;
    for (let index = parts.length - 1; index >= 0; index--)
        patch = { [parts[index]]: patch };
    return patch;
}
function helpOf(info, description) {
    return description !== undefined ? description : (info?.help ?? "");
}
// ---------------------------------------------------------------------- on / off
const SettingToggle = ({ state, path, label, description, disabled }) => {
    const info = settingInfo(state.bundle, path);
    const value = settingValue(state.config, path);
    return (SP_JSX.jsx(DFL.ToggleField, { label: label, description: helpOf(info, description), checked: value === true, disabled: disabled || typeof value !== "boolean", onChange: (checked) => void applyConfig(patchFor(path, checked)) }));
};
// ---------------------------------------------------------------------- numbers
function decimalsOf(step) {
    const text = String(step);
    const dot = text.indexOf(".");
    return dot < 0 ? 0 : text.length - dot - 1;
}
/**
 * A number with limits from the schema. Dragging sends one change when the slider comes to
 * rest, not one per step.
 */
const SettingSlider = ({ state, path, label, description, disabled, step, suffix, }) => {
    const info = settingInfo(state.bundle, path);
    const saved = settingValue(state.config, path);
    const [draft, setDraft] = SP_REACT.useState(null);
    const waiting = SP_REACT.useRef(null);
    const timer = SP_REACT.useRef(null);
    const mounted = SP_REACT.useRef(true);
    const send = () => {
        timer.current = null;
        const value = waiting.current;
        waiting.current = null;
        if (value === null)
            return;
        void applyConfig(patchFor(path, value)).then(() => {
            if (mounted.current && waiting.current === null)
                setDraft(null);
        });
    };
    SP_REACT.useEffect(() => {
        mounted.current = true;
        return () => {
            mounted.current = false;
            if (timer.current !== null) {
                // Leaving the page must neither lose the change nor leave a timer behind.
                clearTimeout(timer.current);
                send();
            }
        };
    }, []);
    if (!info || typeof saved !== "number" || info.min === undefined || info.max === undefined) {
        return SP_JSX.jsx(DFL.Field, { label: label, description: helpOf(info, description), focusable: true });
    }
    const whole = info.type === "int";
    const usedStep = whole ? Math.max(1, Math.round(step)) : step;
    const decimals = decimalsOf(usedStep);
    const clean = (raw) => {
        const snapped = Math.round(raw / usedStep) * usedStep;
        const bounded = Math.min(info.max, Math.max(info.min, snapped));
        return Number(bounded.toFixed(decimals));
    };
    return (SP_JSX.jsx(DFL.SliderField, { label: label, description: helpOf(info, description), value: draft ?? saved, min: info.min, max: info.max, step: usedStep, showValue: true, valueSuffix: suffix, disabled: disabled, onChange: (raw) => {
            const value = clean(raw);
            setDraft(value);
            waiting.current = value;
            if (timer.current !== null)
                clearTimeout(timer.current);
            timer.current = setTimeout(send, 400);
        } }));
};
function allowed(info, value) {
    if (!info)
        return true;
    if (info.choices && typeof value === "string")
        return info.choices.includes(value);
    if (typeof value === "number") {
        if (info.min !== undefined && value < info.min)
            return false;
        if (info.max !== undefined && value > info.max)
            return false;
    }
    return true;
}
/** One choice per whole number the schema allows, for settings with a handful of values. */
function rangeChoices(info, label) {
    const choices = [];
    if (info && info.min !== undefined && info.max !== undefined && info.max - info.min <= 20) {
        for (let value = Math.ceil(info.min); value <= info.max; value++)
            choices.push({ value, label: label(value) });
    }
    return choices;
}
/**
 * One of a few values. Without `choices` the list is the schema's; with them, only those the
 * schema allows are offered. `onPick` may take over (return true) instead of saving at once.
 */
const SettingChoice = ({ state, path, label, description, disabled, choices, showAllHints, onPick }) => {
    const info = settingInfo(state.bundle, path);
    const value = settingValue(state.config, path);
    const base = choices ?? (info?.choices ?? []).map((name) => ({ value: name, label: name }));
    const offered = base.filter((choice) => allowed(info, choice.value));
    if (value !== undefined && !offered.some((choice) => choice.value === value)) {
        // Set from the command line to something this list does not have: keep it selectable.
        offered.unshift({ value, label: value === "" ? "Not set" : String(value) });
    }
    const selected = offered.find((choice) => choice.value === value);
    const help = helpOf(info, description);
    return (SP_JSX.jsx(DFL.DropdownItem, { label: label, description: SP_JSX.jsxs("div", { children: [help && SP_JSX.jsx("div", { children: help }), showAllHints
                    ? offered
                        .filter((choice) => choice.hint)
                        .map((choice) => (SP_JSX.jsxs("div", { style: choice.value === value ? { color: colors.text } : undefined, children: [choice.label, ": ", choice.hint] }, String(choice.value))))
                    : selected?.hint && SP_JSX.jsx("div", { style: { color: colors.text }, children: selected.hint })] }), rgOptions: offered.map((choice) => ({ data: choice.value, label: choice.label })), selectedOption: value, 
        // Steam's Dropdown keeps a selection of its own. Whether it lets go of a pick that was
        // not accepted (a cancelled confirmation, a setting the service rejected) could not be
        // checked without a Steam client, so the row's text is always the setting itself.
        renderButtonValue: () => selected?.label ?? "", disabled: disabled || value === undefined, onChange: (option) => {
            const picked = option.data;
            if (picked === value)
                return;
            if (onPick && onPick(picked))
                return;
            void applyConfig(patchFor(path, picked));
        } }));
};
// ---------------------------------------------------------------------- text
/**
 * A dialog with one text field. Steam's on-screen keyboard opens for its TextField; the
 * value is only sent when the user chooses Save.
 */
const TextEditModal = ({ closeModal, title, label, description, initial, saveLabel, onSave }) => {
    const [value, setValue] = SP_REACT.useState(initial);
    return (SP_JSX.jsx(DFL.ConfirmModal, { strTitle: title, strOKButtonText: saveLabel ?? "Save", strCancelButtonText: "Cancel", onOK: () => onSave(value), closeModal: closeModal, children: SP_JSX.jsx(DFL.TextField, { label: label, description: description, value: value, focusOnMount: true, bShowClearAction: true, onChange: (event) => setValue(event.target.value) }) }));
};
/** A text setting: shows the value; "Edit" opens a dialog with a text field. */
const SettingText = ({ state, path, label, description, disabled, title, emptyLabel, trim = true, }) => {
    const info = settingInfo(state.bundle, path);
    const value = settingValue(state.config, path);
    const text = typeof value === "string" ? value : "";
    const limit = info?.maxlen ? `At most ${info.maxlen} characters.` : "";
    return (SP_JSX.jsx(DFL.Field, { label: label, description: SP_JSX.jsxs("div", { children: [SP_JSX.jsx("div", { style: { color: text ? colors.text : colors.dim, overflowWrap: "anywhere" }, children: text || emptyLabel || "Not set" }), SP_JSX.jsx("div", { children: helpOf(info, description) })] }), children: SP_JSX.jsx(DFL.DialogButton, { style: { width: "auto", minWidth: "7em", padding: "10px 16px" }, disabled: disabled || typeof value !== "string", onClick: () => DFL.showModal(SP_JSX.jsx(TextEditModal, { title: title, label: typeof label === "string" ? label : title, description: [info?.help, limit].filter(Boolean).join(" "), initial: text, onSave: (entered) => {
                    const next = trim ? entered.trim() : entered;
                    // Read the value again: it may have changed while the dialog was open.
                    if (next !== settingValue(store.getState().config, path))
                        void applyConfig(patchFor(path, next));
                } })), children: "Edit" }) }));
};

const AutomationPage = () => {
    const state = useTv();
    const status = state.status;
    const config = state.config;
    const off = config?.enabled === false;
    // With "Never" the service does not look at the displays at all, so the two settings
    // that refine the check do nothing.
    const noCheck = config?.display.require_external === "no";
    const unused = "Not used while Require an external display is set to Never.";
    const noInput = config?.input.method === "none";
    const inputNote = noInput ? "Input switching is turned off; see Input." : "Uses what is set on the Input page.";
    // Displays to choose from: what is connected right now, by the name the display reports.
    const displays = [{ value: "", label: "Any display" }];
    for (const connector of status?.display.connectors ?? []) {
        const name = `${connector.maker} ${connector.model}`.trim();
        if (connector.connected && !connector.internal && name && !displays.some((choice) => choice.value === name)) {
            displays.push({ value: name, label: connector.label });
        }
    }
    return (SP_JSX.jsxs(Page, { state: state, children: [SP_JSX.jsx(Section, { title: "Automatic actions", children: SP_JSX.jsx(SettingToggle, { state: state, path: "enabled", label: "Automatic actions", description: "Master switch for everything below. The buttons in the panel keep working when this is off." }) }), SP_JSX.jsxs(Section, { title: "When SteamOS starts or wakes", children: [SP_JSX.jsx(SettingToggle, { state: state, path: "events.boot_tv_on", label: "Turn the TV on at start", description: "", disabled: off }), SP_JSX.jsx(SettingToggle, { state: state, path: "events.boot_switch_input", label: "Switch the TV input after start", description: inputNote, disabled: off || noInput }), SP_JSX.jsx(SettingToggle, { state: state, path: "events.resume_tv_on", label: "Turn the TV on at wake", description: "", disabled: off }), SP_JSX.jsx(SettingToggle, { state: state, path: "events.resume_switch_input", label: "Switch the TV input after wake", description: inputNote, disabled: off || noInput })] }), SP_JSX.jsxs(Section, { title: "When SteamOS sleeps or shuts down", children: [SP_JSX.jsx(SettingToggle, { state: state, path: "events.suspend_tv_off", label: "Turn the TV off on suspend", description: "", disabled: off }), SP_JSX.jsx(SettingToggle, { state: state, path: "events.shutdown_tv_off", label: "Turn the TV off on shutdown", description: "", disabled: off }), SP_JSX.jsx(SettingToggle, { state: state, path: "events.reboot_tv_off", label: "Turn the TV off on restart", description: "Usually off: the machine is back in a moment.", disabled: off })] }), SP_JSX.jsxs(Section, { title: "Only while the TV is the display", children: [SP_JSX.jsx(DFL.Field, { focusable: true, icon: SP_JSX.jsx(CheckIcon, { done: status ? status.display.gate_ok && !off : null }), label: off
                            ? "Automatic actions are turned off"
                            : status?.display.gate_ok
                                ? "Automatic actions may run right now"
                                : "Automatic actions are skipped right now", description: SP_JSX.jsxs("div", { children: [off && SP_JSX.jsx("div", { children: "Nothing happens by itself until Automatic actions, at the top of this page, is turned on." }), status && SP_JSX.jsxs("div", { children: ["Display check: ", status.display.gate_reason, "."] })] }) }), SP_JSX.jsx(SettingChoice, { state: state, path: "display.require_external", label: "Require an external display", description: "Stops a handheld that wakes on the sofa from turning the TV on.", showAllHints: true, choices: [
                            {
                                value: "auto",
                                label: "Automatic",
                                hint: "a machine with a built-in screen acts only while an external display is connected; one without is assumed to be on the TV.",
                            },
                            { value: "yes", label: "Always", hint: "act only while an external display is connected." },
                            { value: "no", label: "Never", hint: "do not check; always act." },
                        ] }), SP_JSX.jsx(SettingChoice, { state: state, path: "display.edid_match", label: "Only for this display", description: noCheck
                            ? unused
                            : "Act only while this display is connected. The list shows what is connected now; connect the TV to see it here.", choices: displays, disabled: noCheck }), SP_JSX.jsx(SettingSlider, { state: state, path: "display.wait_seconds", label: "Wait for the display", description: noCheck ? unused : undefined, step: 1, suffix: " s", disabled: noCheck })] })] }));
};

const Row = ({ label, children, hint }) => (SP_JSX.jsx(DFL.Field, { focusable: true, label: label, description: hint, padding: "compact", children: children }));
const yes = (on, yesText = "Yes", noText = "No") => (SP_JSX.jsx(Value, { color: on ? colors.ok : colors.dim, children: on ? yesText : noText }));
const IDLE_CHOICES = [
    { value: 0, label: "Never" },
    { value: 30, label: "After 30 seconds" },
    { value: 60, label: "After 1 minute" },
    { value: 300, label: "After 5 minutes" },
    { value: 900, label: "After 15 minutes" },
    { value: 3600, label: "After 1 hour" },
];
const BluetoothPage = () => {
    const state = useTv();
    const bluetooth = state.status?.bluetooth;
    const config = state.config;
    const paired = bluetooth?.paired === true;
    const remoteName = config?.bluetooth.device_name || "SteamOS Remote";
    const connectOutcome = state.outcomes[KEYS.connect];
    const adapters = [{ value: "auto", label: "Automatic" }];
    if (bluetooth?.adapter && bluetooth.adapter !== "auto") {
        adapters.push({ value: bluetooth.adapter, label: bluetooth.adapter });
    }
    return (SP_JSX.jsxs(Page, { state: state, children: [bluetooth && (SP_JSX.jsxs(Section, { title: "Right now", children: [bluetooth.error && (SP_JSX.jsx(DFL.Field, { focusable: true, label: SP_JSX.jsx("span", { style: { color: colors.bad }, children: "Bluetooth problem" }), description: SP_JSX.jsx("span", { style: { color: colors.text, overflowWrap: "anywhere" }, children: bluetooth.error }) })), SP_JSX.jsx(Row, { label: "Adapter", children: SP_JSX.jsx(Value, { color: bluetooth.available && bluetooth.powered ? colors.text : colors.bad, children: !bluetooth.available
                                ? "Not available"
                                : `${bluetooth.adapter || "?"}${bluetooth.adapter_address ? ` (${bluetooth.adapter_address})` : ""}${bluetooth.powered ? "" : ", turned off"}` }) }), SP_JSX.jsx(Row, { label: "TV", children: SP_JSX.jsx(Value, { color: paired ? colors.text : colors.warn, children: paired ? `${bluetooth.tv_name || "TV"} (${bluetooth.tv_address})` : "None paired" }) }), SP_JSX.jsx(Row, { label: "Calling the TV", hint: "Advertising, so that the paired TV connects.", children: bluetooth.pairing ? SP_JSX.jsx(Value, { color: colors.warn, children: "Pairing mode is open" }) : yes(bluetooth.advertising) }), SP_JSX.jsx(Row, { label: "Connected", children: yes(bluetooth.connected) }), SP_JSX.jsx(Row, { label: "Ready for keys", hint: "Connected and settled.", children: yes(bluetooth.ready) }), SP_JSX.jsx(Row, { label: "TV subscribed", hint: "The TV has asked to be sent keys. Lost when SteamOS restarts.", children: yes(bluetooth.subscribed) }), SP_JSX.jsx(Row, { label: "Delivery", children: SP_JSX.jsx(Value, { color: bluetooth.delivery ? colors.text : colors.dim, children: bluetooth.delivery === "bluez"
                                ? "Normal"
                                : bluetooth.delivery === "raw"
                                    ? "Direct (see Advanced)"
                                    : "Not connected" }) }), SP_JSX.jsx(DFL.Field, { label: "Link", description: "Connect now calls the TV and waits for it. Disconnect drops the link.", children: SP_JSX.jsxs(ButtonRow, { style: { flexWrap: "nowrap" }, children: [SP_JSX.jsx(ActionButton, { state: state, actionKey: KEYS.connect, disabled: !paired || bluetooth.ready, onClick: () => void connectTv(), children: "Connect now" }), SP_JSX.jsx(ActionButton, { state: state, actionKey: KEYS.disconnect, disabled: !bluetooth.connected, onClick: () => void disconnectTv(), children: "Disconnect" })] }) }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.connect, contradicted: !!connectOutcome && (connectOutcome.kind === "failed") === bluetooth.ready }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.disconnect, contradicted: bluetooth.connected && state.outcomes[KEYS.disconnect]?.kind === "ok" })] })), SP_JSX.jsxs(Section, { title: "Pairing", children: [SP_JSX.jsx(DFL.Field, { label: paired ? `Paired with ${bluetooth?.tv_name || "the TV"}` : "No TV is paired", description: paired
                            ? "To start over, forget the TV here and remove this remote on the TV as well."
                            : "Pairing mode stays open for two minutes.", children: paired ? (SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: actionDisabled(state, KEYS.forget), onClick: () => DFL.showModal(SP_JSX.jsx(ForgetModal, { tvName: bluetooth?.tv_name ?? "", remoteName: remoteName })), children: "Forget TV" })) : (SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: !bluetooth || !bluetooth.available, onClick: () => DFL.showModal(SP_JSX.jsx(PairModal, {})), children: "Pair TV" })) }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.forget, contradicted: paired }), SP_JSX.jsx(SettingText, { state: state, path: "bluetooth.device_name", label: "Name shown while pairing", title: "Name shown while pairing", description: "What the TV lists this machine as. A new name applies to the next pairing." })] }), SP_JSX.jsxs(Section, { title: "Link", children: [SP_JSX.jsx(SettingToggle, { state: state, path: "bluetooth.reconnect_on_drop", label: "Keep calling the TV" }), SP_JSX.jsx(SettingChoice, { state: state, path: "bluetooth.idle_disconnect_seconds", label: "Disconnect when idle", description: "Drop the link after this long without a key. Helps adapters that cannot reconnect Bluetooth LE controllers while the TV is connected.", choices: IDLE_CHOICES }), SP_JSX.jsx(SettingSlider, { state: state, path: "bluetooth.settle_ms", label: "Settle delay", step: 100, suffix: " ms" }), SP_JSX.jsx(SettingSlider, { state: state, path: "bluetooth.key_press_ms", label: "Key press length", step: 10, suffix: " ms" }), SP_JSX.jsx(SettingSlider, { state: state, path: "bluetooth.key_gap_ms", label: "Pause between macro keys", description: "Gives the TV's menus time to keep up.", step: 50, suffix: " ms" })] }), SP_JSX.jsxs(Section, { title: "Advanced", children: [SP_JSX.jsx(Note, { label: "Rarely needed", children: "Leave these alone unless something does not work." }), SP_JSX.jsx(SettingChoice, { state: state, path: "bluetooth.raw_notify", label: "Direct delivery", description: "After SteamOS restarts, some TVs (Android 11 to 13) never ask for keys again although they are listening. Direct delivery sends keys to them anyway, past the Bluetooth service. On Linux kernels before 6.17 that upsets the kernel's count of Bluetooth packets; this service repairs the count afterwards, but if the repair fell short, controllers and headsets could stall. Designed from source code; not confirmed on a TV.", showAllHints: true, choices: [
                            {
                                value: "auto",
                                label: "Automatic",
                                hint: "Used when the TV needs it, but only on Linux 6.17 or newer, where it has no side effects. On an older kernel it is not used, and a Bluetooth problem at the top of this page says so when a TV needs it.",
                            },
                            { value: "on", label: "On", hint: "Used when the TV needs it, on any kernel." },
                            {
                                value: "off",
                                label: "Off",
                                hint: "Never used. A TV that needs it has to be paired again after each restart of SteamOS.",
                            },
                            { value: "always", label: "Always", hint: "Used for every key, even when the TV has subscribed. For testing." },
                        ] }), SP_JSX.jsx(SettingSlider, { state: state, path: "bluetooth.resubscribe_wait", label: "Wait for the TV to subscribe", step: 0.5, suffix: " s" }), SP_JSX.jsx(SettingChoice, { state: state, path: "bluetooth.wake_beacon", label: "Wake beacon (experimental)", description: "For a TV that sleeps too deeply to connect: while turning it on, also broadcast the signal its own remote sends. TCL's pattern is not public; treat this as an experiment.", choices: [
                            { value: "off", label: "Off" },
                            { value: "mediatek", label: "MediaTek default", hint: "MediaTek's default pattern, built from the TV's address." },
                            { value: "custom", label: "Custom", hint: "The bytes below, captured from your own remote." },
                        ] }), config?.bluetooth.wake_beacon === "custom" && (SP_JSX.jsx(SettingText, { state: state, path: "bluetooth.wake_beacon_data", label: "Custom beacon data", title: "Custom beacon data" })), SP_JSX.jsx(SettingSlider, { state: state, path: "bluetooth.connect_timeout", label: "Wait for the TV when turning it on", step: 1, suffix: " s" }), SP_JSX.jsx(SettingSlider, { state: state, path: "bluetooth.off_connect_timeout", label: "Wait for the TV when turning it off", description: "Keep this short: SteamOS only waits a few seconds before it suspends.", step: 0.5, suffix: " s" }), SP_JSX.jsx(SettingToggle, { state: state, path: "bluetooth.power_on_adapter", label: "Turn Bluetooth on when needed" }), SP_JSX.jsx(SettingChoice, { state: state, path: "bluetooth.adapter", label: "Bluetooth adapter", description: "Leave on Automatic unless the machine has more than one adapter. A change takes effect when the service restarts (Diagnostics).", choices: adapters }), SP_JSX.jsx(SettingChoice, { state: state, path: "bluetooth.gatt_security", label: "Link security", description: "A bring-up aid. A change takes effect when the service restarts (Diagnostics).", choices: [
                            { value: "encrypt", label: "Encrypted", hint: "Normal." },
                            { value: "plain", label: "Not encrypted", hint: "Drops the requirement that the link to the TV is encrypted. For testing only." },
                        ] })] })] }));
};

const LOG_LINES = 100;
const LOG_CHUNK = 20;
const pre = {
    margin: 0,
    fontFamily: "monospace",
    fontSize: "12px",
    lineHeight: "1.45",
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
    color: colors.text,
};
/** Lines of plain text the D-pad can stop on, so that they can be scrolled into view. */
const TextBlock = ({ lines }) => (SP_JSX.jsx(DFL.Focusable, { onActivate: () => undefined, style: { padding: "6px 10px", margin: "2px 0", background: "rgba(0, 0, 0, 0.25)" }, children: SP_JSX.jsx("pre", { style: pre, children: lines.join("\n") }) }));
/**
 * The log. While the service answers it is the service's own; when it does not, its lines
 * in the system journal, which the plugin backend can read: a service that fails at every
 * start says why there and nowhere else.
 */
const Logs = ({ up }) => {
    const [lines, setLines] = SP_REACT.useState(null);
    const [error, setError] = SP_REACT.useState("");
    const [loading, setLoading] = SP_REACT.useState(false);
    const mounted = SP_REACT.useRef(true);
    const busy = SP_REACT.useRef(false);
    const wanted = SP_REACT.useRef(up);
    wanted.current = up;
    const load = () => {
        if (busy.current)
            return;
        busy.current = true;
        setLoading(true);
        const source = wanted.current;
        const request = source ? rpc("logs", { lines: LOG_LINES }) : serviceLog(LOG_LINES);
        void request.then((reply) => {
            busy.current = false;
            if (!mounted.current)
                return;
            setLoading(false);
            if (source !== wanted.current) {
                // The service came up or went away meanwhile: what arrived is the other log.
                load();
                return;
            }
            if (reply.ok) {
                setLines(Array.isArray(reply.result?.lines) ? reply.result.lines.map(String) : []);
                setError("");
            }
            else {
                setLines(null);
                setError(reply.error.message);
            }
        });
    };
    SP_REACT.useEffect(() => {
        mounted.current = true;
        setLines(null);
        setError("");
        load();
        return () => {
            mounted.current = false;
        };
    }, [up]);
    // Newest first, in pieces the D-pad can step through.
    const newestFirst = (lines ?? []).slice().reverse();
    const chunks = [];
    for (let index = 0; index < newestFirst.length; index += LOG_CHUNK)
        chunks.push(newestFirst.slice(index, index + LOG_CHUNK));
    return (SP_JSX.jsxs(Section, { title: "Log", children: [SP_JSX.jsx(DFL.Field, { label: up ? "Recent log lines" : "The service's last lines in the system journal", description: error ? (SP_JSX.jsxs("span", { style: { color: colors.bad }, children: ["The log could not be read: ", error] })) : lines === null ? (loading ? "Reading." : "Not loaded.") : up ? (`The last ${lines.length} lines, newest first. The service writes down every step of every action.`) : (`The service does not answer, so this is what the system kept of it: the last ${lines.length} lines, newest first. If the service fails to start, the reason is here.`), children: SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: loading, onClick: () => load(), children: loading ? "Loading…" : "Refresh" }) }), chunks.map((chunk, index) => (SP_JSX.jsx(TextBlock, { lines: chunk }, index))), lines !== null && lines.length === 0 && !error && (SP_JSX.jsx(Note, { label: up ? "The log is empty" : "The system journal has no lines from the service" }))] }));
};
const CHECK_WORD = {
    ok: "OK",
    warn: "Warning",
    fail: "Problem",
    unknown: "Not known",
};
const CHECK_COLOR = {
    ok: colors.ok,
    warn: colors.warn,
    fail: colors.bad,
    unknown: colors.dim,
};
function checkSummary(report) {
    const count = (status) => report.checks.filter((check) => check.status === status).length;
    const parts = [
        count("fail") ? `${count("fail")} ${count("fail") === 1 ? "problem" : "problems"}` : "",
        count("warn") ? `${count("warn")} ${count("warn") === 1 ? "warning" : "warnings"}` : "",
        count("unknown") ? `${count("unknown")} not known` : "",
    ].filter(Boolean);
    return parts.length ? `${report.checks.length} checks: ${parts.join(", ")}.` : `All ${report.checks.length} checks passed.`;
}
/**
 * The doctor's report, which the command line prints for "tcl-me-remo doctor". Asked for
 * with a button and never by itself: it starts a dozen system tools.
 */
const SetupCheck = () => {
    const [report, setReport] = SP_REACT.useState(null);
    const [error, setError] = SP_REACT.useState("");
    const [running, setRunning] = SP_REACT.useState(false);
    const mounted = SP_REACT.useRef(true);
    SP_REACT.useEffect(() => {
        mounted.current = true;
        return () => {
            mounted.current = false;
        };
    }, []);
    const run = () => {
        if (running)
            return;
        setRunning(true);
        void doctor().then((reply) => {
            if (!mounted.current)
                return;
            setRunning(false);
            if (reply.ok) {
                setReport(reply.result);
                setError("");
            }
            else {
                setReport(null);
                setError(reply.error.message);
            }
        });
    };
    return (SP_JSX.jsxs(Section, { title: "Check the set-up", children: [SP_JSX.jsx(DFL.Field, { label: "Check Bluetooth, the service, the pairing and the suspend hooks", description: error ? (SP_JSX.jsxs("span", { style: { color: colors.bad }, children: ["The check could not be made: ", error] })) : report ? (SP_JSX.jsx("span", { style: { color: CHECK_COLOR[report.worst] ?? colors.text }, children: checkSummary(report) })) : ("Says what is wrong in plain words. It only looks; nothing is changed. Works when the service does not. Include a screenshot of the result in a bug report."), children: SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: running, onClick: () => run(), children: running ? "Checking…" : report ? "Check again" : "Run check" }) }), report && report.system.length > 0 && SP_JSX.jsx(TextBlock, { lines: report.system }), report?.checks.map((check, index) => (SP_JSX.jsx(DFL.Field, { focusable: true, padding: "compact", label: SP_JSX.jsxs("span", { children: [SP_JSX.jsx("span", { style: { color: CHECK_COLOR[check.status] ?? colors.dim }, children: CHECK_WORD[check.status] ?? check.status }), ` · ${check.name}`] }), description: SP_JSX.jsx("span", { style: { color: colors.text, overflowWrap: "anywhere" }, children: check.detail }) }, index))), report && report.details.length > 0 && SP_JSX.jsx(TextBlock, { lines: report.details })] }));
};
const DiagnosticsPage = () => {
    const state = useTv();
    const { status, service, config } = state;
    // What is installed can change behind the panel's back; look again when the page opens.
    SP_REACT.useEffect(() => {
        void store.refreshService();
    }, []);
    const serviceWorking = [KEYS.install, KEYS.restart, KEYS.remove].some((key) => key in state.pending);
    const history = (status?.history ?? []).slice().reverse();
    const times = (count) => (count === 1 ? "Once" : `${count} times`);
    const onKeys = [
        { value: "WAKE", label: "Wake", hint: "Does nothing if the TV is already on." },
        { value: "POWER", label: "Power toggle", hint: "Turns a TV that is already on off. Avoid." },
    ];
    const offKeys = [
        { value: "SLEEP", label: "Sleep", hint: "Does nothing if the TV is already off." },
        { value: "POWER", label: "Power toggle", hint: "Turns a TV that is already off on. Avoid." },
    ];
    return (SP_JSX.jsxs(Page, { state: state, worksOffline: true, children: [SP_JSX.jsxs(Section, { title: "Background service", children: [service ? (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.Field, { focusable: true, label: "Installed", padding: "compact", children: SP_JSX.jsx(Value, { color: service.installed ? colors.ok : colors.bad, children: service.installed
                                        ? `Yes, version ${service.installed_version || "unknown"}${service.install_dir ? ` in ${service.install_dir}` : ""}`
                                        : "No" }) }), SP_JSX.jsx(DFL.Field, { focusable: true, label: "Running", padding: "compact", children: SP_JSX.jsx(Value, { color: service.active ? colors.ok : colors.bad, children: service.active
                                        ? status
                                            ? `Yes, version ${status.version}, for ${duration(status.uptime)}`
                                            : "Yes, but it does not answer"
                                        : "No" }) }), SP_JSX.jsx(DFL.Field, { focusable: true, label: "Starts with SteamOS", padding: "compact", children: SP_JSX.jsx(Value, { color: service.enabled ? colors.ok : colors.bad, children: service.enabled ? "Yes" : "No" }) }), SP_JSX.jsx(DFL.Field, { focusable: true, label: "Version in this plugin", padding: "compact", children: SP_JSX.jsxs(Value, { color: service.installed && service.needs_update ? colors.warn : colors.text, children: [service.bundled_version || "unknown", service.installed && service.needs_update ? " (differs from the installed one; Repair updates it)" : ""] }) }), service.problems.map((problem, index) => (SP_JSX.jsx(DFL.Field, { focusable: true, padding: "compact", label: SP_JSX.jsx("span", { style: { color: colors.bad }, children: "Problem" }), description: SP_JSX.jsx("span", { style: { color: colors.text, overflowWrap: "anywhere" }, children: problem }) }, index)))] })) : (SP_JSX.jsx(Note, { label: state.serviceError ? "The service's state could not be checked" : "Checking what is installed", children: state.serviceError })), SP_JSX.jsx(DFL.Field, { label: "Repair, restart, remove", description: "Repair installs, updates and starts the service; it changes nothing that is already right. Restart makes the TV connect and subscribe anew, so use it only when something is stuck.", childrenLayout: "below", children: SP_JSX.jsxs(ButtonRow, { children: [SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: serviceWorking, onClick: () => void installService(), children: KEYS.install in state.pending ? "Working…" : service && !service.installed ? "Install" : "Repair" }), SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: serviceWorking || !service?.installed || (status !== null && status.busy !== ""), onClick: () => void restartService(), children: KEYS.restart in state.pending ? "Working…" : "Restart" }), SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: serviceWorking || !service?.installed, onClick: () => DFL.showModal(SP_JSX.jsx(RemoveServiceModal, {})), children: KEYS.remove in state.pending ? "Working…" : "Remove" })] }) }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.install }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.restart }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.remove })] }), SP_JSX.jsx(Section, { title: "Updates", children: SP_JSX.jsx(DFL.Field, { focusable: true, label: `This plugin is version ${service?.bundled_version || "unknown"}`, description: SP_JSX.jsxs("div", { children: [SP_JSX.jsx("div", { children: "It does not look for updates, and nothing is updated by itself. To update, install the plugin again from the address it came from: Decky settings > Developer > Install Plugin from URL. Type the address exactly; the same address always gives the newest version." }), SP_JSX.jsx("div", { style: { ...pre, fontSize: "13px", margin: "6px 0" }, children: UPDATE_ADDRESS }), SP_JSX.jsx("div", { children: "The address is not on screen while you type it: write it down or have it on a phone before you leave this page. Decky then asks \"Install tcl-me-remo\"; if it shows any other name, the end of the address is wrong, so cancel and correct it." }), SP_JSX.jsx("div", { style: { marginTop: "6px" }, children: "Settings and the pairing with the TV are kept. The background service is updated when the new plugin loads. An update that changes the service restarts it, which for the TV is like a restart of SteamOS: if keys do not arrive afterwards, the Bluetooth page says why." })] }) }) }), SP_JSX.jsx(SetupCheck, {}), status && status.config_problems.length > 0 && (SP_JSX.jsxs(Section, { title: "Settings problems", children: [status.config_problems.map((problem, index) => (SP_JSX.jsx(DFL.Field, { focusable: true, label: SP_JSX.jsx("span", { style: { color: colors.warn }, children: "Problem in the settings file" }), description: SP_JSX.jsx("span", { style: { color: colors.text, overflowWrap: "anywhere" }, children: problem }) }, index))), status.config_problems.some((problem) => !problem.includes(".broken")) && (SP_JSX.jsx(Note, { children: "Changing any setting writes a clean settings file." })), status.config_problems.some((problem) => problem.includes(".broken")) && (SP_JSX.jsx(Note, { label: "The service is running on default settings", children: "Changing a setting does not clear this, and does not bring the old settings back: they are in the file named above. The way on from here: set things up again on these pages, and pair the TV again if it shows as unpaired. This notice then ends when the service next restarts (Restart, at the top of this page). Getting the old settings back instead means mending that file by hand, which takes a terminal in Desktop Mode: see \"A settings file that was moved aside\" in the README." }))] })), status && (SP_JSX.jsxs(Section, { title: "What happened", children: [history.length === 0 && (SP_JSX.jsx(Note, { label: "Nothing yet", children: "Actions show up here with every step that was tried." })), history.map((record) => (SP_JSX.jsx(RecordView, { record: record, startOpen: false }, `${record.time}-${record.name}-${record.trigger}`)))] })), SP_JSX.jsx(Logs, { up: !!status }), status && config && (SP_JSX.jsxs(Section, { title: "Advanced", children: [SP_JSX.jsx(SettingChoice, { state: state, path: "power.on_key", label: "Key that turns the TV on", description: "Other keys can be set from the command line.", choices: onKeys }), SP_JSX.jsx(SettingChoice, { state: state, path: "power.on_repeat", label: "Send the on key", description: "", choices: rangeChoices(settingInfo(state.bundle, "power.on_repeat"), times) }), SP_JSX.jsx(SettingChoice, { state: state, path: "power.off_key", label: "Key that turns the TV off", description: "Other keys can be set from the command line.", choices: offKeys }), SP_JSX.jsx(SettingChoice, { state: state, path: "power.off_repeat", label: "Send the off key", description: "", choices: rangeChoices(settingInfo(state.bundle, "power.off_repeat"), times) }), SP_JSX.jsx(SettingSlider, { state: state, path: "power.repeat_gap", label: "Pause between repeats", description: "Only matters when a key is sent more than once.", step: 0.1, suffix: " s" }), SP_JSX.jsx(SettingChoice, { state: state, path: "log_level", label: "Log detail", description: "", choices: [
                            { value: "DEBUG", label: "Everything", hint: "For bug reports." },
                            { value: "INFO", label: "Normal" },
                            { value: "WARNING", label: "Problems only" },
                            { value: "ERROR", label: "Errors only" },
                        ] })] }))] }));
};

const code$1 = { fontFamily: "monospace", color: colors.text, overflowWrap: "anywhere" };
const paragraph = { margin: "0 0 10px 0" };
// Keys offered in the palette, in this order, when the service knows them. Power keys are
// left out on purpose: a macro is for menus.
const PALETTE_KEYS = ["F3", "TV", "HOME", "BACK", "MENU", "UP", "DOWN", "LEFT", "RIGHT", "OK"];
const PALETTE_WAITS = ["500ms", "1s", "2s"];
const WAIT_RE = /^\d+(\.\d+)?(ms|s)$/i;
function tokens(text) {
    return text.trim().split(/[\s,;]+/).filter(Boolean);
}
/** Add a step; pressing the same key again turns LEFT into LEFT*2 instead of repeating it. */
function appendToken(text, token) {
    const parts = tokens(text);
    const last = parts[parts.length - 1];
    if (last && !WAIT_RE.test(token)) {
        const match = /^(.+?)(?:\*(\d+))?$/.exec(last);
        if (match && match[1].toUpperCase() === token.toUpperCase()) {
            parts[parts.length - 1] = `${match[1]}*${Number(match[2] ?? 1) + 1}`;
            return parts.join(" ");
        }
    }
    parts.push(token);
    return parts.join(" ");
}
/** Make the last key repeat once per HDMI number: RIGHT becomes RIGHT*{port}. */
function repeatPerPort(text) {
    const parts = tokens(text);
    const last = parts[parts.length - 1];
    if (!last || WAIT_RE.test(last))
        return text;
    parts[parts.length - 1] = `${last.replace(/\*[^*]*$/, "")}*{port}`;
    return parts.join(" ");
}
function dropLast(text) {
    return tokens(text).slice(0, -1).join(" ");
}
/** What the service makes of each preset for the chosen HDMI port. */
function usePresetChecks(presets, state) {
    const port = state.config?.input.hdmi_port ?? 1;
    const [checks, setChecks] = SP_REACT.useState({ port, byName: {} });
    const asked = SP_REACT.useRef("");
    const up = !!state.status;
    // The answers depend on the port (sent along) and on the key timing the service has, so
    // after a settings change they are asked for again once the change has arrived there.
    const settling = state.settling;
    const wanted = [
        presets.map((preset) => preset.name).join(","),
        port,
        state.config?.bluetooth.key_press_ms,
        state.config?.bluetooth.key_gap_ms,
    ].join("|");
    SP_REACT.useEffect(() => {
        if (!up) {
            asked.current = "";
            return undefined;
        }
        if (settling || asked.current === wanted)
            return undefined;
        asked.current = wanted;
        let live = true;
        let open = presets.length;
        for (const preset of presets) {
            void rpc("check_macro", { macro: preset.template, port }).then((reply) => {
                open -= 1;
                if (!live || !reply.ok)
                    return;
                setChecks((known) => ({
                    port,
                    byName: { ...(known.port === port ? known.byName : {}), [preset.name]: reply.result },
                }));
            });
        }
        return () => {
            live = false;
            // Interrupted before every answer was in: ask again next time.
            if (open > 0 && asked.current === wanted)
                asked.current = "";
        };
    }, [wanted, up, settling]);
    return checks.port === port ? checks : { port, byName: {} };
}
/** Validates macro text with the service a moment after the user stops changing it. */
function useMacroCheck(text, port) {
    const [result, setResult] = SP_REACT.useState(null);
    SP_REACT.useEffect(() => {
        if (!text.trim())
            return undefined;
        let live = true;
        const timer = setTimeout(() => {
            void rpc("check_macro", { macro: text, port }).then((reply) => {
                if (!live)
                    return;
                setResult({ text, port, check: reply.ok ? reply.result : { ok: false, error: reply.error.message } });
            });
        }, 350);
        return () => {
            live = false;
            clearTimeout(timer);
        };
    }, [text, port]);
    return result && result.text === text && result.port === port ? result.check : null;
}
/**
 * When what is configured gets sent, in words. "TV on" switches the input as well, and the
 * automatic part follows the switches on the Automation page.
 */
function whenSent(config) {
    const byHand = "This happens when you press TV on or Switch input";
    if (!config.enabled)
        return `${byHand}. It does not happen by itself: automatic actions are turned off.`;
    const atStart = config.events.boot_switch_input;
    const atWake = config.events.resume_switch_input;
    if (atStart && atWake)
        return `${byHand}, and by itself after SteamOS starts or wakes.`;
    if (atStart)
        return `${byHand}, and by itself after SteamOS starts. After a wake it is turned off (see Automation).`;
    if (atWake)
        return `${byHand}, and by itself after SteamOS wakes. After a start it is turned off (see Automation).`;
    return `${byHand}. It does not happen by itself: switching the input after start and after wake is turned off (see Automation).`;
}
/**
 * Shown before input switching is turned on: what will be sent, and when. It follows the
 * live state, and it cannot be agreed to while a settings change (another HDMI port, another
 * preset) has not yet arrived in what the service reports it will send.
 */
const MethodConfirm = ({ closeModal, method }) => {
    const state = useTv();
    const { status, config } = state;
    const apply = () => void applyConfig({ input: { method } });
    const title = method === "macro" ? "Switch input with a Bluetooth macro?" : "Switch input over Wi-Fi?";
    if (!status || !config) {
        return (SP_JSX.jsx(ConfirmSendModal, { closeModal: closeModal, title: title, okLabel: "Turn on", blocked: "The background service does not answer, so what would be sent is not known.", onOK: apply }));
    }
    const input = status.input;
    const port = config.input.hdmi_port;
    const waiting = state.settling || input.hdmi_port !== port;
    const checking = SP_JSX.jsx("p", { style: { ...paragraph, color: colors.dim }, children: "Checking what will be sent." });
    const macroProblem = input.macro_error
        ? `The macro is not valid: ${input.macro_error}`
        : input.macro.trim()
            ? ""
            : "No macro is set. Choose a preset or save your own macro first.";
    const macroLines = waiting ? (checking) : (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx("p", { style: { ...paragraph, ...code$1 }, children: input.macro || "(nothing)" }), SP_JSX.jsxs("p", { style: paragraph, children: ["That takes about ", seconds(input.macro_seconds), ". After turning the TV on, the machine first waits", " ", seconds(config.input.delay_after_wake), " for it to start."] })] }));
    if (method === "macro") {
        return (SP_JSX.jsxs(ConfirmSendModal, { closeModal: closeModal, title: title, okLabel: "Turn on", blocked: macroProblem, waiting: waiting, onOK: apply, children: [SP_JSX.jsxs("p", { style: paragraph, children: ["These keys are sent to the TV for HDMI ", port, ":"] }), macroLines, SP_JSX.jsx("p", { style: paragraph, children: whenSent(config) }), SP_JSX.jsx("p", { style: { ...paragraph, color: colors.warn }, children: "The presets come from owners' reports and are not confirmed. Use Try on this page and watch the TV before turning this on." })] }));
    }
    const wifi = status.wifi;
    const byLink = config.wifi.input_mode === "app_link";
    const fallback = config.input.wifi_fallback_to_macro && input.macro.trim() !== "" && !input.macro_error;
    const blocked = !wifi.available
        ? "The Wi-Fi assist is not available in this build."
        : !wifi.paired || !wifi.enabled || !wifi.host
            ? "The Wi-Fi assist is not set up yet. Pair it on the Wi-Fi assist page first."
            : byLink && !input.wifi_link
                ? `No link is set for HDMI ${port}. Choose a link set on the Wi-Fi assist page.`
                : "";
    return (SP_JSX.jsxs(ConfirmSendModal, { closeModal: closeModal, title: title, okLabel: "Turn on", blocked: blocked, waiting: waiting, onOK: apply, children: [SP_JSX.jsxs("p", { style: paragraph, children: [wifi.name || wifi.host || "The TV", " is asked over Wi-Fi to", " ", byLink ? `open this link for HDMI ${port}:` : `press its HDMI ${port} key.`] }), byLink ? (waiting ? (checking) : (SP_JSX.jsx("p", { style: { ...paragraph, ...code$1 }, children: input.wifi_link || "(no link set)" }))) : (SP_JSX.jsx("p", { style: { ...paragraph, color: colors.warn }, children: "TCL TVs are reported to ignore that key; opening a link is reported to work." })), SP_JSX.jsx("p", { style: paragraph, children: whenSent(config) }), fallback ? (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx("p", { style: paragraph, children: "If the TV does not answer over Wi-Fi, these keys are sent over Bluetooth instead:" }), macroLines] })) : (SP_JSX.jsx("p", { style: paragraph, children: "If the TV does not answer over Wi-Fi, nothing else is tried." })), SP_JSX.jsx("p", { style: { ...paragraph, color: colors.warn }, children: "The link sets come from owners' reports and are not confirmed." })] }));
};
/** What the chosen preset or macro is used for right now: a selection is not yet a use. */
function useOf(config) {
    if (config?.input.method === "macro")
        return "active";
    if (config?.input.method === "wifi" && config.input.wifi_fallback_to_macro)
        return "fallback";
    return "idle";
}
const SelectedBadge = ({ use }) => (SP_JSX.jsx("span", { style: { color: use === "idle" ? colors.dim : colors.ok, marginLeft: "10px" }, children: use === "active" ? "In use" : use === "fallback" ? "In use as the fallback" : "Selected" }));
const PresetRow = ({ state, preset, port, check, }) => {
    const inUse = state.config?.input.preset === preset.name;
    const key = `tv.preset.${preset.name}`;
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.Field, { label: SP_JSX.jsxs("span", { children: [preset.title, inUse && SP_JSX.jsx(SelectedBadge, { use: useOf(state.config) })] }), description: SP_JSX.jsxs("div", { children: [SP_JSX.jsx("div", { children: preset.description }), check?.ok ? (SP_JSX.jsxs("div", { children: ["On HDMI ", port, " it sends ", SP_JSX.jsx("span", { style: code$1, children: check.rendered }), " (about ", seconds(check.seconds ?? 0), ")."] })) : (SP_JSX.jsxs("div", { children: ["Pattern: ", SP_JSX.jsx("span", { style: code$1, children: preset.template })] }))] }), children: SP_JSX.jsxs(ButtonRow, { style: { flexWrap: "nowrap" }, children: [SP_JSX.jsx(ActionButton, { state: state, actionKey: key, onClick: () => void runMacro(key, preset.template), children: "Try" }), SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: inUse, onClick: () => void applyConfig({ input: { preset: preset.name } }), children: "Use" })] }) }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: key })] }));
};
const MacroEditor = ({ state }) => {
    const saved = state.config?.input.macro ?? "";
    const custom = state.config?.input.preset === "custom";
    const [draft, setDraft] = SP_REACT.useState(null);
    const text = draft ?? saved;
    const port = state.config?.input.hdmi_port ?? 1;
    const check = useMacroCheck(text, port);
    const empty = text.trim() === "";
    const valid = !empty && check?.ok === true;
    const known = new Set((state.bundle?.keys ?? []).map((key) => key.name));
    const keys = PALETTE_KEYS.filter((name) => known.has(name));
    const limit = settingInfo(state.bundle, "input.macro")?.maxlen;
    const add = (token) => setDraft(appendToken(text, token));
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.TextField, { label: "Macro", description: "Steps separated by spaces. HOME presses a key, DOWN*3 presses it three times, HOME~1.5 holds it, 2s waits, {port} is the HDMI number.", value: text, onChange: (event) => setDraft(event.target.value) }), SP_JSX.jsx(DFL.Field, { focusable: true, bottomSeparator: "none", label: empty ? ("Nothing entered yet") : !check ? ("Checking") : check.ok ? (SP_JSX.jsx("span", { style: { color: colors.ok }, children: "This macro is valid" })) : (SP_JSX.jsx("span", { style: { color: colors.bad }, children: "This macro is not valid" })), description: empty ? ("Type a macro, or build one with the buttons below.") : !check ? ("") : check.ok ? (SP_JSX.jsxs("span", { children: ["On HDMI ", port, " it sends ", SP_JSX.jsx("span", { style: code$1, children: check.rendered }), " and takes about", " ", seconds(check.seconds ?? 0), "."] })) : (SP_JSX.jsx("span", { style: { color: colors.text, overflowWrap: "anywhere" }, children: check.error })) }), SP_JSX.jsx(DFL.Field, { label: "Add a step", description: `Each button adds to the end of the macro.${limit ? ` At most ${limit} characters.` : ""}`, childrenLayout: "below", children: SP_JSX.jsxs(DFL.Focusable, { style: { display: "flex", flexDirection: "column", gap: "8px" }, children: [SP_JSX.jsx(ButtonRow, { children: keys.map((name) => (SP_JSX.jsx(DFL.DialogButton, { style: { ...rowButton, minWidth: "4.5em" }, onClick: () => add(name), children: name }, name))) }), SP_JSX.jsxs(ButtonRow, { children: [PALETTE_WAITS.map((wait) => (SP_JSX.jsxs(DFL.DialogButton, { style: { ...rowButton, minWidth: "4.5em" }, onClick: () => add(wait), children: ["Wait ", wait] }, wait))), SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: empty, onClick: () => setDraft(repeatPerPort(text)), children: "Repeat once per HDMI number" }), SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: empty, onClick: () => setDraft(dropLast(text)), children: "Remove last" }), SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: empty, onClick: () => setDraft(""), children: "Clear" })] })] }) }), SP_JSX.jsx(DFL.Field, { label: !custom
                    ? "Use your own macro"
                    : useOf(state.config) === "active"
                        ? "Your macro is in use"
                        : useOf(state.config) === "fallback"
                            ? "Your macro is in use as the fallback"
                            : "Your macro is selected", description: "Try it and watch the TV. Save stores it and makes it the macro that is used.", childrenLayout: "below", children: SP_JSX.jsxs(ButtonRow, { children: [SP_JSX.jsx(ActionButton, { state: state, actionKey: KEYS.macro, disabled: !valid, onClick: () => void runMacro(KEYS.macro, text), children: "Try it now" }), SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: !valid || (custom && text.trim() === saved), onClick: () => void applyConfig({ input: { macro: check?.normalized ?? text.trim(), preset: "custom" } }).then((result) => {
                                if (result.ok)
                                    setDraft(null);
                            }), children: "Save" }), SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: draft === null || draft === saved, onClick: () => setDraft(null), children: "Discard changes" })] }) }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.macro })] }));
};
const InputPage = () => {
    const state = useTv();
    const { status, config, bundle } = state;
    const presets = bundle?.presets ?? [];
    const checks = usePresetChecks(presets, state);
    const ports = rangeChoices(settingInfo(bundle, "input.hdmi_port"), (port) => `HDMI ${port}`);
    const method = config?.input.method;
    const input = status?.input;
    return (SP_JSX.jsxs(Page, { state: state, children: [SP_JSX.jsxs(Section, { title: "Getting to the right input", children: [SP_JSX.jsx(Note, { label: "Recommended: let the TV do it", children: "On the TV, set Settings > System > Power & Energy > Power On Behavior to Last Input Used. The TV then comes back on the input it showed last. This needs no key presses, and it makes everything below a fallback rather than a necessity." }), SP_JSX.jsx(SettingChoice, { state: state, path: "input.hdmi_port", label: "This machine is on", description: "The HDMI port of the TV that this machine is plugged into.", choices: ports }), SP_JSX.jsx(SettingChoice, { state: state, path: "input.method", label: "Switch the input", description: "", showAllHints: true, choices: [
                            { value: "none", label: "Off", hint: "nothing is sent; the TV decides." },
                            {
                                value: "macro",
                                label: "Bluetooth macro",
                                hint: "presses the keys you would: open the source list, move to the entry, OK.",
                            },
                            {
                                value: "wifi",
                                label: "Wi-Fi",
                                hint: "asks the TV for the input directly; needs the Wi-Fi assist and the TV's network.",
                            },
                        ], onPick: (value) => {
                            if (value !== "macro" && value !== "wifi")
                                return false;
                            DFL.showModal(SP_JSX.jsx(MethodConfirm, { method: value }));
                            return true;
                        } }), input && (SP_JSX.jsx(DFL.Field, { focusable: true, label: "What is sent", description: method === "none" ? ("Nothing. Input switching is off.") : method === "wifi" ? (SP_JSX.jsxs("div", { children: [SP_JSX.jsxs("div", { children: ["Over Wi-Fi: ", SP_JSX.jsx("span", { style: code$1, children: input.wifi_link || `the HDMI ${input.hdmi_port} key` })] }), config?.input.wifi_fallback_to_macro && input.macro && (SP_JSX.jsxs("div", { children: ["If that fails, over Bluetooth: ", SP_JSX.jsx("span", { style: code$1, children: input.macro })] }))] })) : input.macro_error ? (SP_JSX.jsxs("span", { style: { color: colors.bad }, children: ["The macro is not valid: ", input.macro_error] })) : (SP_JSX.jsxs("span", { children: [SP_JSX.jsx("span", { style: code$1, children: input.macro || "(no macro set)" }), " (about ", seconds(input.macro_seconds), ")"] })) })), method === "wifi" && (SP_JSX.jsx(SettingToggle, { state: state, path: "input.wifi_fallback_to_macro", label: "Fall back to the Bluetooth macro" })), SP_JSX.jsx(SettingSlider, { state: state, path: "input.delay_after_wake", label: "Wait after turning the TV on", description: "A TV that has just woken ignores keys for a few seconds.", step: 0.5, suffix: " s" }), SP_JSX.jsx(DFL.Field, { label: "Switch input now", description: method === "none" ? "Turn input switching on first." : "Sends what is shown above, right now.", children: SP_JSX.jsx(ActionButton, { state: state, actionKey: KEYS.switchInput, disabled: method === "none", onClick: () => void switchInput(), children: "Switch input now" }) }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.switchInput })] }), SP_JSX.jsxs(Section, { title: "Bluetooth macro presets", children: [SP_JSX.jsx(Note, { label: "Reported, not confirmed", children: "Android TVs have no key for a given HDMI input, so a macro walks through the TV's source list. These presets are starting points from owners' reports. Switch the TV to another input, press Try and watch what happens. F3 is reported to open the source list on one TCL model; if nothing opens, try TV in your own macro." }), presets.map((preset) => (SP_JSX.jsx(PresetRow, { state: state, preset: preset, port: checks.port, check: checks.byName[preset.name] }, preset.name)))] }), SP_JSX.jsxs(Section, { title: "Your own macro", children: [SP_JSX.jsx(MacroEditor, { state: state }), SP_JSX.jsx(Note, { label: "Finding what your TV needs", children: "Look at the source list: is it a row or a column, what comes before HDMI 1, are there entries you never use? The list does not wrap around, so go to one end with more presses than it has entries, then count back. Hide unused inputs in the TV's settings and set the others to On rather than Auto, so the list keeps its length." })] })] }));
};

// How many presses may wait for the TV before further ones are refused.
const MAX_WAITING = 4;
const icon = { display: "block", margin: "0 auto" };
/** An icon with its name underneath: an arrow explains itself, a speaker with an x less so. */
const named = (symbol, text) => (SP_JSX.jsxs("span", { style: { display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }, children: [symbol, SP_JSX.jsx("span", { children: text })] }));
const ROWS = [
    [
        { key: "WAKE", label: "Wake", name: "Wake" },
        null,
        { key: "SLEEP", label: "Sleep", name: "Sleep" },
    ],
    [null, { key: "UP", label: SP_JSX.jsx(FaChevronUp, { style: icon }), name: "Up" }, null],
    [
        { key: "LEFT", label: SP_JSX.jsx(FaChevronLeft, { style: icon }), name: "Left" },
        { key: "OK", label: "OK", name: "OK" },
        { key: "RIGHT", label: SP_JSX.jsx(FaChevronRight, { style: icon }), name: "Right" },
    ],
    [null, { key: "DOWN", label: SP_JSX.jsx(FaChevronDown, { style: icon }), name: "Down" }, null],
    [
        { key: "BACK", label: named(SP_JSX.jsx(FaArrowLeft, {}), "Back"), name: "Back" },
        { key: "HOME", label: named(SP_JSX.jsx(FaHome, {}), "Home"), name: "Home" },
        { key: "MENU", label: named(SP_JSX.jsx(FaBars, {}), "Menu"), name: "Menu" },
    ],
    [
        { key: "VOL_DOWN", label: named(SP_JSX.jsx(FaVolumeDown, {}), "Vol −"), name: "Volume down" },
        { key: "MUTE", label: named(SP_JSX.jsx(FaVolumeMute, {}), "Mute"), name: "Mute" },
        { key: "VOL_UP", label: named(SP_JSX.jsx(FaVolumeUp, {}), "Vol +"), name: "Volume up" },
    ],
    [null, { key: "F3", label: "Source list", name: "Source list (F3)" }, null],
];
const cell = { width: "9em", minWidth: 0, height: "3.2em", flex: "0 0 auto", padding: "0 8px" };
const RemotePage = () => {
    const state = useTv();
    const [waiting, setWaiting] = SP_REACT.useState(0);
    const [note, setNote] = SP_REACT.useState(null);
    const chain = SP_REACT.useRef(Promise.resolve());
    const count = SP_REACT.useRef(0);
    const round = SP_REACT.useRef(0);
    const mounted = SP_REACT.useRef(true);
    SP_REACT.useEffect(() => {
        mounted.current = true;
        return () => {
            mounted.current = false;
        };
    }, []);
    const bluetooth = state.status?.bluetooth;
    const paired = bluetooth?.paired === true;
    const busy = busyReason(state);
    const known = new Set((state.bundle?.keys ?? []).map((entry) => entry.name));
    const press = (entry) => {
        if (count.current >= MAX_WAITING) {
            setNote({ failed: true, text: "The TV has not taken the earlier keys yet. Wait a moment." });
            return;
        }
        const mine = round.current;
        count.current += 1;
        setWaiting(count.current);
        // One key at a time, in the order they were pressed.
        chain.current = chain.current
            .then(async () => {
            // An earlier key failed: do not keep hammering a TV that is not there.
            if (mine !== round.current)
                return;
            const reply = await rpc("send_key", { key: entry.key });
            if (!reply.ok)
                round.current += 1;
            if (mounted.current) {
                setNote(reply.ok
                    ? { failed: false, text: `Sent ${entry.name}` }
                    : { failed: true, text: `${entry.name} was not sent: ${reply.error.message}` });
            }
        })
            .catch((reason) => console.warn("tcl-me-remo: send_key handling failed", reason))
            .then(() => {
            count.current -= 1;
            if (mounted.current)
                setWaiting(count.current);
            if (count.current === 0)
                store.poll();
        });
    };
    return (SP_JSX.jsx(Page, { state: state, children: SP_JSX.jsxs(Section, { title: "Remote", children: [SP_JSX.jsx(DFL.Field, { focusable: true, label: !paired
                        ? "No TV is paired"
                        : bluetooth?.ready
                            ? `${bluetooth.tv_name || "The TV"} is connected`
                            : `${bluetooth?.tv_name || "The TV"} is not connected right now`, description: SP_JSX.jsxs("div", { children: [SP_JSX.jsx("div", { children: !paired
                                    ? "Pair a TV on the Setup page first."
                                    : bluetooth?.ready
                                        ? "Keys are sent as you press them."
                                        : "The first key waits for the TV to connect, which can take a few seconds." }), busy && SP_JSX.jsxs("div", { style: { color: colors.warn }, children: ["The service is busy (", busy, "); keys cannot be sent until it is done."] }), SP_JSX.jsx("div", { style: {
                                    color: note?.failed ? colors.bad : colors.text,
                                    minHeight: "1.4em",
                                    fontWeight: note?.failed ? "bold" : "normal",
                                    overflowWrap: "anywhere",
                                }, children: waiting > 0 && !note?.failed ? "Sending…" : (note?.text ?? "") })] }) }), SP_JSX.jsx(DFL.Focusable, { style: { display: "flex", flexDirection: "column", gap: "8px", alignItems: "center", padding: "12px 0" }, children: ROWS.map((row, index) => (SP_JSX.jsx(ButtonRow, { style: { flexWrap: "nowrap", justifyContent: "center" }, children: row.map((entry, column) => entry && known.has(entry.key) ? (SP_JSX.jsx(DFL.DialogButton, { style: cell, disabled: !paired || busy !== "", onClick: () => press(entry), children: entry.label }, entry.key)) : (SP_JSX.jsx("div", { style: { ...cell, visibility: "hidden" } }, `gap-${column}`))) }, index))) }), SP_JSX.jsx(DFL.Field, { focusable: true, bottomSeparator: "none", label: "About these keys", description: "Wake and Sleep do nothing when the TV is already in that state. Source list sends F3, which is reported to open the input list on one TCL model. On an Android TV, any key also wakes the screen." })] }) }));
};

const SetupPage = () => {
    const state = useTv();
    const status = state.status;
    const bluetooth = status?.bluetooth;
    const paired = bluetooth?.paired === true;
    const ready = bluetooth?.ready === true;
    const remoteName = state.config?.bluetooth.device_name || "SteamOS Remote";
    // Without the service nothing is known about the TV; that is not the same as "not paired".
    const unknown = "Not known while the service is not answering";
    const connectOutcome = state.outcomes[KEYS.connect];
    return (SP_JSX.jsxs(Page, { state: state, worksOffline: true, children: [SP_JSX.jsx(Section, { title: "1. The background service", children: SP_JSX.jsx(DFL.Field, { focusable: true, icon: SP_JSX.jsx(CheckIcon, { done: !!status }), label: status ? "The service is running" : "The service is not answering", description: status
                        ? `Version ${status.version}. It keeps the Bluetooth link to the TV and reacts when SteamOS sleeps and wakes.`
                        : "See the message at the top of this page." }) }), SP_JSX.jsxs(Section, { title: "2. Prepare the TV", children: [SP_JSX.jsx(Note, { icon: SP_JSX.jsx(CheckIcon, { done: null }), label: "Quick start and Screenless service: On", children: "On the TV: Settings > System > Power & Energy. On 2025 models, set Standby Mode to the screenless option instead. This keeps the TV reachable while its screen is off; it uses a little more standby power." }), SP_JSX.jsx(Note, { icon: SP_JSX.jsx(CheckIcon, { done: null }), label: "Power On Behavior: Last Input Used", children: "Same menu. The TV then comes back on the input it showed last, which is the most reliable way to land on this machine." }), SP_JSX.jsx(Note, { icon: SP_JSX.jsx(CheckIcon, { done: null }), label: "Never confirm Shut down", children: "A long press of the TV remote's power button offers Shut down. A TV that is shut down can only be turned on with its own remote or the button on the set." }), SP_JSX.jsx(Note, { label: "Not yet confirmed on real hardware", children: "Which standby modes a TCL TV can be woken from is put together from manuals and owners' reports. The first thing to test after pairing is whether your TV wakes." })] }), SP_JSX.jsxs(Section, { title: "3. Pair", children: [SP_JSX.jsx(DFL.Field, { icon: SP_JSX.jsx(CheckIcon, { done: status ? paired : null }), label: !bluetooth ? `Pairing: ${unknown.toLowerCase()}` : paired ? `Paired with ${bluetooth.tv_name || "the TV"}` : "No TV is paired", description: !bluetooth ? ("See the message at the top of this page.") : paired ? ("Pair once; the pairing survives restarts of both sides.") : (SP_JSX.jsxs("div", { children: [SP_JSX.jsxs("div", { children: ["Choose Pair TV. Then on the TV open Settings > Remotes & Accessories > Pair accessory and pick ", remoteName, ". Pairing mode stays open for two minutes."] }), bluetooth && !bluetooth.available && (SP_JSX.jsx("div", { style: { color: colors.bad }, children: bluetooth.error || "Bluetooth is not available." }))] })), children: paired ? (SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: actionDisabled(state, KEYS.forget), onClick: () => DFL.showModal(SP_JSX.jsx(ForgetModal, { tvName: bluetooth?.tv_name ?? "", remoteName: remoteName })), children: "Forget TV" })) : (SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: !bluetooth || !bluetooth.available, onClick: () => DFL.showModal(SP_JSX.jsx(PairModal, {})), children: "Pair TV" })) }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.forget, contradicted: paired }), SP_JSX.jsx(DFL.Field, { icon: SP_JSX.jsx(CheckIcon, { done: status && paired ? ready : null }), label: !bluetooth
                            ? `Connection: ${unknown.toLowerCase()}`
                            : ready
                                ? "The TV is connected"
                                : "The TV is not connected right now", description: !bluetooth
                            ? ""
                            : ready
                                ? "Keys can be sent right away."
                                : "A paired TV connects by itself while it is on or in screenless standby.", children: SP_JSX.jsx(ActionButton, { state: state, actionKey: KEYS.connect, disabled: !paired || ready, onClick: () => void connectTv(), children: "Connect now" }) }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.connect, contradicted: !!bluetooth && !!connectOutcome && (connectOutcome.kind === "failed") === ready })] }), SP_JSX.jsxs(Section, { title: "4. Test", children: [SP_JSX.jsx(DFL.Field, { label: "Try what happens on wake and on sleep", description: SP_JSX.jsxs("div", { children: [SP_JSX.jsx("div", { children: "Test wake runs exactly what runs when SteamOS starts or wakes; Test sleep what runs when it suspends. SteamOS itself stays awake." }), status && !status.display.gate_ok && (SP_JSX.jsxs("div", { style: { color: colors.warn }, children: ["Right now both would be skipped: ", status.display.gate_reason, ". See Automation."] })), status && !status.enabled && (SP_JSX.jsx("div", { style: { color: colors.warn }, children: "Automatic actions are off, so both would be skipped." })), !status && SP_JSX.jsx("div", { style: { color: colors.warn }, children: "The tests need the background service." }), status && !paired && SP_JSX.jsx("div", { style: { color: colors.warn }, children: "Pair a TV first." })] }), childrenLayout: "below", children: SP_JSX.jsxs(ButtonRow, { children: [SP_JSX.jsx(ActionButton, { state: state, actionKey: KEYS.testSleep, disabled: !paired, onClick: () => void testEvent("suspend"), children: "Test sleep" }), SP_JSX.jsx(ActionButton, { state: state, actionKey: KEYS.testWake, disabled: !paired, onClick: () => void testEvent("resume"), children: "Test wake" })] }) }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.testSleep }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.testWake }), SP_JSX.jsx(Note, { label: "Then try again after an hour", children: "Some sets change standby state a while after they are switched off. Let the TV sit in standby for an hour and run Test wake once more." }), SP_JSX.jsx(Note, { label: "If the TV does not wake", children: "Re-check Screenless service or Standby Mode; that is the cause in most cases. Models with a MediaTek chipset may need the experimental wake beacon (Bluetooth page). A TV whose network stays up in standby may also answer a wake request from the Wi-Fi assist." }), SP_JSX.jsx(Note, { label: "If the TV wakes when it should not", children: "Turn off Keep calling the TV on the Bluetooth page." })] })] }));
};

const code = { fontFamily: "monospace", color: colors.text, overflowWrap: "anywhere" };
function pair(host, name) {
    DFL.showModal(SP_JSX.jsx(WifiPairModal, { host: host, name: name }));
}
/** A chosen link set is only in use while the input is switched over Wi-Fi. */
const Selected = ({ active }) => (SP_JSX.jsx("span", { style: { color: active ? colors.ok : colors.dim, marginLeft: "10px" }, children: active ? "In use" : "Selected" }));
const LinkRow = ({ state, preset }) => {
    const config = state.config;
    const port = config?.input.hdmi_port ?? 1;
    const inUse = config?.wifi.link_preset === preset.name;
    const active = config?.input.method === "wifi";
    const link = preset.links[String(port)];
    return (SP_JSX.jsx(DFL.Field, { label: SP_JSX.jsxs("span", { children: [preset.title, inUse && SP_JSX.jsx(Selected, { active: active })] }), description: SP_JSX.jsxs("div", { children: [SP_JSX.jsx("div", { children: preset.description }), SP_JSX.jsx("div", { children: link ? (SP_JSX.jsxs("span", { children: ["For HDMI ", port, " it opens ", SP_JSX.jsx("span", { style: code, children: link })] })) : (SP_JSX.jsxs("span", { style: { color: colors.warn }, children: ["It has no link for HDMI ", port, "."] })) })] }), children: SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: inUse, onClick: () => void applyConfig({ wifi: { link_preset: preset.name } }), children: "Use" }) }));
};
const WifiPage = () => {
    const state = useTv();
    const [found, setFound] = SP_REACT.useState(null);
    const { status, config, bundle } = state;
    const wifi = status?.wifi;
    const port = config?.input.hdmi_port ?? 1;
    const method = config?.input.method;
    const customLink = config?.wifi.link_preset === "custom";
    const byLink = config?.wifi.input_mode === "app_link";
    // The link the service says it will open; it lags a settings change by a moment.
    const sentLink = state.settling ? "" : (status?.input.wifi_link ?? "");
    const testBlocked = !wifi?.paired
        ? "Pair the Wi-Fi assist first."
        : !wifi.enabled
            ? "Turn on Use the Wi-Fi assist first."
            : !wifi.host
                ? "Set the TV address first."
                : state.settling
                    ? "Checking what will be sent."
                    : byLink && !sentLink
                        ? `No link is set for HDMI ${port}. Choose a link set above.`
                        : "";
    if (wifi && !wifi.available) {
        return (SP_JSX.jsx(Page, { state: state, children: SP_JSX.jsx(Section, { title: "Wi-Fi assist", children: SP_JSX.jsx(Note, { label: "Not available", children: "The Wi-Fi assist is not available in this build of the service." }) }) }));
    }
    return (SP_JSX.jsxs(Page, { state: state, children: [SP_JSX.jsxs(Section, { title: "Wi-Fi assist", children: [SP_JSX.jsx(Note, { label: "Optional", children: "The machine pairs with the TV over your network, the way the Google TV phone app does. It can then ask for an input directly, which is more reliable than a key macro, and turn the TV on or off when Bluetooth cannot reach it. It only works while the TV's network is up: when the TV is on, or in standby with Screenless service." }), SP_JSX.jsxs(DFL.Field, { icon: SP_JSX.jsx(CheckIcon, { done: wifi ? wifi.paired : null }), label: wifi?.paired
                            ? `Paired with ${wifi.name || wifi.host || "the TV"}`
                            : wifi?.pairing
                                ? `Pairing with ${wifi.pairing_host}`
                                : "Not paired", description: wifi?.paired
                            ? `Address: ${wifi.host || "not set"}. ${wifi.enabled ? "The assist is on." : "The assist is off."}${wifi.pairing ? ` A new pairing with ${wifi.pairing_host} is open.` : ""}`
                            : wifi?.pairing
                                ? "The TV is showing a code. Choose Pair to enter it, or cancel."
                                : "Search for the TV or enter its address below, then pair.", children: [wifi?.pairing && !wifi.paired && (SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: KEYS.wifiPairCancel in state.pending, onClick: () => void wifiPairCancel(), children: "Cancel pairing" })), wifi?.paired && (SP_JSX.jsxs(ButtonRow, { style: { flexWrap: "nowrap" }, children: [SP_JSX.jsx(ActionButton, { state: state, actionKey: KEYS.wifiTest, onClick: () => void wifiTest(), children: "Check connection" }), wifi.pairing && (SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: KEYS.wifiPairCancel in state.pending, onClick: () => void wifiPairCancel(), children: "Cancel pairing" })), SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: actionDisabled(state, KEYS.wifiUnpair), onClick: () => DFL.showModal(SP_JSX.jsx(ConfirmSendModal, { title: "Remove the Wi-Fi pairing?", okLabel: "Unpair", blocked: "", onOK: () => void wifiUnpair(), children: SP_JSX.jsx("p", { style: { margin: 0 }, children: "The Wi-Fi assist is turned off and has to be paired again before it can be used. The Bluetooth pairing is not touched." }) })), children: "Unpair" })] }))] }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.wifiPairCancel, contradicted: wifi?.pairing === true }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.wifiTest, contradicted: wifi?.paired === false }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.wifiUnpair, contradicted: wifi?.paired === true }), SP_JSX.jsx(SettingToggle, { state: state, path: "wifi.enabled", label: "Use the Wi-Fi assist", description: wifi?.paired ? "Off keeps the pairing but does not use it." : "Pair first; pairing turns this on.", disabled: !wifi?.paired && config?.wifi.enabled !== true })] }), SP_JSX.jsxs(Section, { title: "Find the TV", children: [SP_JSX.jsx(DFL.Field, { label: "Search the network", description: "Looks for Google TV devices for a few seconds. The TV must be on.", children: SP_JSX.jsx(ActionButton, { state: state, actionKey: KEYS.wifiDiscover, onClick: () => void wifiDiscover().then((result) => setFound(result.devices)), children: "Search" }) }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.wifiDiscover }), (found ?? []).map((device) => (SP_JSX.jsx(DFL.Field, { label: device.name || device.host, description: `Address: ${device.host}`, children: SP_JSX.jsx(DFL.DialogButton, { style: rowButton, onClick: () => pair(device.host, device.name), children: "Pair" }) }, `${device.host}:${device.port}`))), found !== null && found.length === 0 && !(KEYS.wifiDiscover in state.pending) && (SP_JSX.jsx(Note, { label: "No TV answered", children: "Check that the TV is on and on the same network, or enter its address below." })), SP_JSX.jsx(SettingText, { state: state, path: "wifi.host", label: "TV address", title: "TV address", emptyLabel: "Not set" }), SP_JSX.jsx(DFL.Field, { label: "Pair with this address", description: "The TV shows a code; you enter it here.", children: SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: !config?.wifi.host, onClick: () => pair(config?.wifi.host ?? "", config?.wifi.name || undefined), children: wifi?.paired ? "Pair again" : "Pair" }) })] }), SP_JSX.jsxs(Section, { title: "Switching the input", children: [SP_JSX.jsx(SettingChoice, { state: state, path: "wifi.input_mode", label: "How the input is asked for", description: "", choices: [
                            {
                                value: "app_link",
                                label: "Open a link",
                                hint: "Opens the TV's own link for the input. Reported to work on TCL TVs.",
                            },
                            { value: "keycode", label: "HDMI key", hint: "Sends the HDMI key code. TCL TVs are reported to ignore it." },
                        ] }), byLink && (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(Note, { label: "Link sets: reported, not confirmed", children: "Each TV chipset names its inputs differently. These sets come from owners' reports. Pick the one for your model and test it." }), (bundle?.link_presets ?? []).map((preset) => (SP_JSX.jsx(LinkRow, { state: state, preset: preset }, preset.name))), SP_JSX.jsx(DFL.Field, { label: SP_JSX.jsxs("span", { children: ["Your own link", customLink && SP_JSX.jsx(Selected, { active: method === "wifi" })] }), description: "Uses the link below; {port} stands for the HDMI number.", children: SP_JSX.jsx(DFL.DialogButton, { style: rowButton, disabled: customLink, onClick: () => void applyConfig({ wifi: { link_preset: "custom" } }), children: "Use" }) }), SP_JSX.jsx(SettingText, { state: state, path: "wifi.app_link", label: "Custom link", title: "Custom link", description: "Only used when Your own link is chosen." })] })), SP_JSX.jsx(SettingSlider, { state: state, path: "wifi.connect_timeout", label: "Keep trying to switch for", description: "A TV that has just woken can take a while to answer on the network. Only used when switching the input.", step: 1, suffix: " s" }), SP_JSX.jsx(DFL.Field, { label: `Test: switch to HDMI ${port} now`, description: SP_JSX.jsxs("div", { children: [testBlocked ? (SP_JSX.jsx("div", { style: { color: colors.warn }, children: testBlocked })) : (SP_JSX.jsxs("div", { children: ["Sends: ", SP_JSX.jsx("span", { style: code, children: byLink ? sentLink : `the HDMI ${port} key` })] })), SP_JSX.jsx("div", { children: "Asks over Wi-Fi only, whatever is chosen on the Input page, and tries nothing else if the TV does not answer. Switch the TV to another input first and watch what happens." })] }), children: SP_JSX.jsx(ActionButton, { state: state, actionKey: KEYS.wifiSwitch, disabled: testBlocked !== "", onClick: () => void switchInputOverWifi(), children: "Switch input now" }) }), SP_JSX.jsx(OutcomeView, { state: state, actionKey: KEYS.wifiSwitch })] }), SP_JSX.jsx(Section, { title: "Power", children: SP_JSX.jsx(SettingToggle, { state: state, path: "wifi.power_fallback", label: "Use Wi-Fi for power when Bluetooth fails", description: "Turns the TV on or off over Wi-Fi when the Bluetooth link is not available." }) })] }));
};

/**
 * The full-screen settings. Holds the status polling for as long as it is on screen, without
 * subscribing to it: each page listens for itself, and Steam's SidebarNavigation is handed
 * the same list of pages on every render instead of a new one each second.
 */
const SettingsRouter = () => {
    useHold();
    const pages = SP_REACT.useMemo(() => [
        { title: "Setup", content: SP_JSX.jsx(SetupPage, {}), route: `${ROUTE}/setup` },
        { title: "Automation", content: SP_JSX.jsx(AutomationPage, {}), route: `${ROUTE}/automation` },
        { title: "Input", content: SP_JSX.jsx(InputPage, {}), route: `${ROUTE}/input` },
        { title: "Remote", content: SP_JSX.jsx(RemotePage, {}), route: `${ROUTE}/remote` },
        "separator",
        { title: "Bluetooth", content: SP_JSX.jsx(BluetoothPage, {}), route: `${ROUTE}/bluetooth` },
        { title: "Wi-Fi assist", content: SP_JSX.jsx(WifiPage, {}), route: `${ROUTE}/wifi` },
        { title: "Diagnostics", content: SP_JSX.jsx(DiagnosticsPage, {}), route: `${ROUTE}/diagnostics` },
    ], []);
    return SP_JSX.jsx(DFL.SidebarNavigation, { title: "TV remote", showTitle: true, pages: pages });
};

var index = definePlugin(() => {
    // Runs once per load of the Steam UI. The panel and the settings page start and stop the
    // status polling themselves when they mount and unmount; nothing runs in between.
    routerHook.addRoute(ROUTE, SettingsRouter);
    return {
        name: "tcl-me-remo",
        titleView: SP_JSX.jsx(TitleView, {}),
        content: SP_JSX.jsx(QuickPanel, {}),
        icon: SP_JSX.jsx(FaTv, {}),
        onDismount() {
            routerHook.removeRoute(ROUTE);
            store.shutdown();
        },
    };
});

export { index as default };
//# sourceMappingURL=index.js.map
