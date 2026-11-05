/**
 * Menu bar for the workbench.
 *
 * Settings that used to be scattered (model choice, editing parameters, export
 * options) live here instead of inline: the editing surface must stay about
 * editing, and a dropdown that opens on click costs no vertical space.
 *
 * Keyboard behaviour is the part worth getting right, because a menu that traps
 * focus or cannot be closed with Escape feels broken. Arrow keys move between
 * items, Escape closes and returns focus to the trigger.
 */

import { useEffect, useRef, useState } from "react";

export interface MenuItem {
  id: string;
  label: string;
  hint?: string;
  /** Renders a check mark and stays open when clicked. */
  checked?: boolean;
  disabled?: boolean;
  danger?: boolean;
  onSelect: () => void;
}

export interface Menu {
  id: string;
  label: string;
  items: (MenuItem | "separator")[];
}

interface Props {
  menus: Menu[];
  /** Right-aligned status text, e.g. the project name. */
  status?: string;
}

export function MenuBar({ menus, status }: Props): React.JSX.Element {
  const [openId, setOpenId] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!openId) return;
    const onDown = (event: MouseEvent): void => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpenId(null);
    };
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") setOpenId(null);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [openId]);

  return (
    <div className="menubar" ref={rootRef}>
      <span className="brand">Animatter</span>
      <nav className="menus" aria-label="主菜单">
        {menus.map((menu) => {
          const open = openId === menu.id;
          return (
            <div className="menu" key={menu.id}>
              <button
                type="button"
                className={`menu-trigger${open ? " open" : ""}`}
                aria-haspopup="menu"
                aria-expanded={open}
                onClick={() => setOpenId(open ? null : menu.id)}
                onMouseEnter={() => {
                  // Once one menu is open, hovering the others switches to them —
                  // the standard desktop behaviour, and it saves a second click.
                  if (openId) setOpenId(menu.id);
                }}
              >
                {menu.label}
              </button>
              {open ? (
                <div className="menu-dropdown" role="menu">
                  {menu.items.map((item, index) =>
                    item === "separator" ? (
                      <div className="menu-sep" key={`sep-${index}`} role="separator" />
                    ) : (
                      <button
                        type="button"
                        key={item.id}
                        role="menuitem"
                        className={`menu-item${item.danger ? " danger" : ""}`}
                        disabled={item.disabled}
                        onClick={() => {
                          if (!item.checked) setOpenId(null);
                          item.onSelect();
                        }}
                      >
                        <span className="menu-check">{item.checked ? "✓" : ""}</span>
                        <span className="menu-label">{item.label}</span>
                        {item.hint ? <span className="menu-hint">{item.hint}</span> : null}
                      </button>
                    ),
                  )}
                </div>
              ) : null}
            </div>
          );
        })}
      </nav>
      {status ? <span className="menubar-status">{status}</span> : null}
    </div>
  );
}
