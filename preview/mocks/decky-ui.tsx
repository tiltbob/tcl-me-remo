// Stand-ins for the @decky/ui components the plugin uses: plain HTML with the same prop
// names, typed against the real package so a prop the real component does not have cannot
// creep in. They imitate how the components look and what they call, not Steam's gamepad
// focus handling, which only a real Steam client can show.

import type * as Real from "@decky/ui";
import type { CSSProperties, FC, KeyboardEvent, MouseEvent, ReactNode } from "react";
import { cloneElement, isValidElement, useEffect, useRef, useState } from "react";

import { closeModal as closeHostModal, getHost, navigate, openModal, record, replaceModal, useHost } from "../host";

function classes(...names: (string | false | undefined)[]): string {
  return names.filter(Boolean).join(" ");
}

function onEnter(handler: (() => void) | undefined) {
  return (event: KeyboardEvent<HTMLElement>) => {
    if (handler && event.target === event.currentTarget && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      handler();
    }
  };
}

function plain(node: ReactNode): string | undefined {
  return typeof node === "string" ? node : undefined;
}

// ---------------------------------------------------------------------- Quick Access Menu

export const staticClasses = { Title: "qam-title", PanelSectionTitle: "qam-section-title" } as unknown as typeof Real.staticClasses;

export const PanelSection: typeof Real.PanelSection = ({ title, children }) => (
  <div className="qam-section">
    {title && <div className="qam-section-title">{title}</div>}
    {children}
  </div>
);

export const PanelSectionRow: typeof Real.PanelSectionRow = ({ children }) => <div className="qam-row">{children}</div>;

// ---------------------------------------------------------------------- rows

interface RowProps {
  kind: string;
  label?: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  children?: ReactNode;
  below?: boolean;
  focusable?: boolean;
  disabled?: boolean;
  padding?: string;
  separator?: string;
  className?: string;
  onActivate?: () => void;
}

const Row: FC<RowProps> = ({
  kind,
  label,
  description,
  icon,
  children,
  below,
  focusable,
  disabled,
  padding,
  separator,
  className,
  onActivate,
}) => (
  <div
    className={classes(
      "field",
      `field-${kind}`,
      below && "field-below",
      focusable && "field-focusable",
      disabled && "field-disabled",
      padding && `field-pad-${padding}`,
      separator === "none" && "field-no-separator",
      className,
    )}
    data-label={plain(label)}
    tabIndex={focusable ? 0 : undefined}
    onClick={focusable && onActivate ? () => onActivate() : undefined}
    onKeyDown={focusable ? onEnter(onActivate) : undefined}
  >
    <div className="field-main">
      {icon && <div className="field-icon">{icon}</div>}
      {label !== undefined && label !== null && <div className="field-label">{label}</div>}
      {children !== undefined && children !== null && children !== false && (
        <div className="field-children">{children}</div>
      )}
    </div>
    {description !== undefined && description !== null && description !== "" && (
      <div className="field-description">{description}</div>
    )}
  </div>
);

export const Field: typeof Real.Field = ({
  label,
  description,
  icon,
  children,
  childrenLayout,
  focusable,
  disabled,
  padding,
  bottomSeparator,
  className,
  onActivate,
  onClick,
}) => {
  const activate = onActivate ?? onClick;
  return (
    <Row
      kind="plain"
      label={label}
      description={description}
      icon={icon}
      below={childrenLayout === "below"}
      focusable={focusable || !!activate}
      disabled={disabled}
      padding={padding}
      separator={bottomSeparator}
      className={className}
      onActivate={activate ? () => activate(new MouseEvent("click")) : undefined}
    >
      {children}
    </Row>
  );
};

export const ButtonItem: typeof Real.ButtonItem = ({ label, description, icon, children, layout, disabled, onClick, bottomSeparator }) => (
  <Row kind="button" label={label} description={description} icon={icon} below={layout === "below"} disabled={disabled} separator={bottomSeparator}>
    <button
      type="button"
      className="dialog-button"
      disabled={disabled}
      onClick={(event) => onClick?.(event.nativeEvent)}
    >
      {children}
    </button>
  </Row>
);

export const ToggleField: typeof Real.ToggleField = ({ label, description, icon, checked, disabled, onChange, bottomSeparator }) => (
  <Row kind="toggle" label={label} description={description} icon={icon} disabled={disabled} separator={bottomSeparator}>
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={plain(label)}
      className={classes("toggle", checked && "toggle-on")}
      disabled={disabled}
      onClick={() => onChange?.(!checked)}
    >
      <span className="toggle-knob" />
    </button>
  </Row>
);

export const SliderField: typeof Real.SliderField = ({
  label,
  description,
  icon,
  value,
  min = 0,
  max = 100,
  step = 1,
  showValue,
  valueSuffix,
  disabled,
  onChange,
  bottomSeparator,
}) => (
  <Row kind="slider" label={label} description={description} icon={icon} below={true} disabled={disabled} separator={bottomSeparator}>
    <div className="slider">
      <input
        type="range"
        aria-label={plain(label)}
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange?.(Number(event.target.value))}
      />
      {showValue && (
        <span className="slider-value">
          {value}
          {valueSuffix}
        </span>
      )}
    </div>
  </Row>
);

export const Dropdown: typeof Real.Dropdown = ({
  rgOptions,
  selectedOption,
  disabled,
  onChange,
  strDefaultLabel,
  menuLabel,
  renderButtonValue,
}) => {
  const options = rgOptions.filter((option) => !("options" in option && option.options));
  const selected = options.findIndex((option) => option.data === selectedOption);
  // Steam's Dropdown is a class with a selection of its own, and whether it lets go of a pick
  // that the plugin did not accept is not known. The stand-in assumes the worse case: what it
  // shows follows the last pick, and follows `selectedOption` only when that prop changes.
  // A plugin that wants the truth on screen has to say so through renderButtonValue.
  const [held, setHeld] = useState<unknown>(selectedOption);
  const lastProp = useRef<unknown>(selectedOption);
  useEffect(() => {
    if (lastProp.current !== selectedOption) {
      lastProp.current = selectedOption;
      setHeld(selectedOption);
    }
  }, [selectedOption]);
  const shown = lastProp.current !== selectedOption ? selectedOption : held;
  const own = options.find((option) => option.data === shown)?.label ?? strDefaultLabel ?? "";
  return (
    <span className={classes("dropdown", disabled && "dropdown-disabled")}>
      <span className="dropdown-value">{renderButtonValue ? renderButtonValue(own) : own}</span>
      <span className="dropdown-arrow" aria-hidden="true" />
      {/* The native control does the picking; it sits invisibly on top of the label. */}
      <select
        aria-label={menuLabel}
        disabled={disabled}
        value={selected}
        onChange={(event) => {
          const option = options[Number(event.target.value)];
          if (!option) return;
          setHeld(option.data);
          onChange?.(option as Real.SingleDropdownOption);
        }}
      >
        {selected < 0 && <option value={-1}>{strDefaultLabel ?? ""}</option>}
        {options.map((option, index) => (
          <option key={index} value={index}>
            {option.label}
          </option>
        ))}
      </select>
    </span>
  );
};

export const DropdownItem: typeof Real.DropdownItem = ({ label, description, icon, bottomSeparator, ...dropdown }) => (
  <Row kind="dropdown" label={label} description={description} icon={icon} disabled={dropdown.disabled} separator={bottomSeparator}>
    <Dropdown {...dropdown} menuLabel={dropdown.menuLabel ?? plain(label)} />
  </Row>
);

export const TextField: typeof Real.TextField = ({
  label,
  description,
  value,
  disabled,
  focusOnMount,
  bShowClearAction,
  placeholder,
  onChange,
}) => {
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (focusOnMount) input.current?.focus();
  }, []);
  return (
    <div className={classes("field", "field-text", disabled && "field-disabled")} data-label={plain(label)}>
      {label && <div className="field-label">{label}</div>}
      <div className="text-input">
        <input
          ref={input}
          type="text"
          aria-label={plain(label)}
          value={value ?? ""}
          placeholder={placeholder}
          disabled={disabled}
          onChange={onChange}
        />
        {bShowClearAction && value ? <span className="text-clear">×</span> : null}
      </div>
      {description && <div className="field-description">{description}</div>}
    </div>
  );
};

// ---------------------------------------------------------------------- focus and buttons

export const Focusable: typeof Real.Focusable = ({ children, style, className, onActivate, onClick }) => (
  <div
    className={classes("focusable", onActivate && "focusable-active", className)}
    style={style}
    tabIndex={onActivate ? 0 : undefined}
    onClick={onClick}
    onKeyDown={onEnter(onActivate ? () => onActivate(new CustomEvent("activate")) : undefined)}
  >
    {children}
  </div>
);

const button =
  (extra: string): typeof Real.DialogButton =>
  ({ children, style, className, disabled, onClick }) => (
    <button
      type="button"
      className={classes("dialog-button", extra, className)}
      style={style}
      disabled={disabled}
      onClick={(event: MouseEvent<HTMLButtonElement>) => onClick?.(event.nativeEvent)}
    >
      {children}
    </button>
  );

export const DialogButton = button("");
export const DialogButtonPrimary = button("dialog-button-primary");
export const DialogButtonSecondary = button("");

const block =
  (name: string): FC<{ children?: ReactNode; style?: CSSProperties; className?: string }> =>
  ({ children, style, className }) => (
    <div className={classes(name, className)} style={style}>
      {children}
    </div>
  );

export const DialogBody = block("dialog-body") as typeof Real.DialogBody;
export const DialogControlsSection = block("dialog-section") as typeof Real.DialogControlsSection;
export const DialogControlsSectionHeader = block("dialog-section-header") as typeof Real.DialogControlsSectionHeader;
export const DialogHeader = block("dialog-header") as typeof Real.DialogHeader;
export const DialogBodyText = block("dialog-body-text") as typeof Real.DialogBodyText;
export const DialogFooter = block("dialog-footer") as typeof Real.DialogFooter;

export const Spinner: typeof Real.Spinner = ({ style }: { style?: CSSProperties }) => (
  <span className="spinner" style={{ display: "inline-block", ...style }} />
);

// ---------------------------------------------------------------------- modals

export const showModal: typeof Real.showModal = (modal) => {
  const { id, closed } = openModal(modal);
  return {
    Close: () => closeHostModal(id),
    ClosedPromise: closed,
    Update: (next) => replaceModal(id, next),
  };
};

/** Rendered once by the harness: shows what showModal() opened, each with closeModal injected. */
export const ModalHost: FC = () => {
  const { modals } = useHost();
  return (
    <>
      {modals.map((modal) => (
        <div key={modal.id} className="modal-backdrop">
          {isValidElement<{ closeModal?: () => void }>(modal.element)
            ? cloneElement(modal.element, { closeModal: () => closeHostModal(modal.id) })
            : modal.element}
        </div>
      ))}
    </>
  );
};

const Dialog: FC<{ children?: ReactNode; onEscape: () => void; destructive?: boolean }> = ({
  children,
  onEscape,
  destructive,
}) => {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // Like Steam: focus lands inside the dialog, on the first thing that can take it.
    const first = box.current?.querySelector<HTMLElement>("input:not(:disabled), button:not(:disabled)");
    (first ?? box.current)?.focus();
  }, []);
  return (
    <div
      ref={box}
      role="dialog"
      tabIndex={-1}
      className={classes("modal", destructive && "modal-destructive")}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          onEscape();
        }
      }}
    >
      {children}
    </div>
  );
};

export const ModalRoot: FC<Real.ModalRootProps> = ({ children, onCancel, closeModal, bDestructiveWarning }) => (
  <Dialog destructive={bDestructiveWarning} onEscape={() => (onCancel ? onCancel() : closeModal?.())}>
    {children}
  </Dialog>
);

export const ConfirmModal: typeof Real.ConfirmModal = ({
  children,
  onOK,
  onCancel,
  closeModal,
  strTitle,
  strDescription,
  strOKButtonText,
  strCancelButtonText,
  bAlertDialog,
  bOKDisabled,
  bCancelDisabled,
  bDestructiveWarning,
}) => {
  // Steam's ConfirmModal runs the handler and then closes itself.
  const ok = () => {
    onOK?.();
    closeModal?.();
  };
  const cancel = () => {
    onCancel?.();
    closeModal?.();
  };
  return (
    <Dialog destructive={bDestructiveWarning} onEscape={cancel}>
      {strTitle && <div className="dialog-header">{strTitle}</div>}
      <div className="dialog-body-text">
        {strDescription && <div>{strDescription}</div>}
        {children}
      </div>
      <div className="dialog-footer">
        <div className="dialog-footer-buttons">
          <button type="button" className="dialog-button dialog-button-primary" disabled={bOKDisabled} onClick={ok}>
            {strOKButtonText ?? "OK"}
          </button>
          {!bAlertDialog && (
            <button type="button" className="dialog-button" disabled={bCancelDisabled} onClick={cancel}>
              {strCancelButtonText ?? "Cancel"}
            </button>
          )}
        </div>
      </div>
    </Dialog>
  );
};

// ---------------------------------------------------------------------- navigation

export const Navigation = {
  Navigate: (path: string) => navigate(path),
  NavigateBack: () => navigate(""),
  CloseSideMenus: () => {
    record.sideMenusClosed += 1;
  },
  OpenQuickAccessMenu: () => navigate(""),
} as unknown as typeof Real.Navigation;

export const SidebarNavigation: typeof Real.SidebarNavigation = ({ title, showTitle, pages }) => {
  const { location } = useHost();
  // The tests check that the plugin does not hand Steam a new list of pages every second.
  const lastPages = useRef<typeof pages | null>(null);
  if (lastPages.current !== pages) {
    lastPages.current = pages;
    record.sidebarPageLists += 1;
  }
  const real = pages.filter((page): page is Real.SidebarNavigationPage => page !== "separator");
  const current = real.find((page) => page.route === location) ?? real[0];
  return (
    <div className="sidebar-layout">
      <nav className="sidebar">
        {showTitle && title && <div className="sidebar-title">{title}</div>}
        {pages.map((page, index) =>
          page === "separator" ? (
            <div key={index} className="sidebar-separator" />
          ) : (
            <button
              key={index}
              type="button"
              className={classes("sidebar-item", page === current && "sidebar-item-active")}
              onClick={() => navigate(page.route ?? getHost().location)}
            >
              {page.title}
            </button>
          ),
        )}
      </nav>
      <main className="sidebar-content">
        {current && !current.hideTitle && <div className="sidebar-page-title">{current.title}</div>}
        {current?.content}
      </main>
    </div>
  );
};
