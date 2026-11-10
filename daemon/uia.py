"""UI Automation lookups via comtypes. Used only inside the daemon's IPC thread."""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("screen-overlay.uia")

CONTROL_TYPES = {
    "Button": 50000, "Calendar": 50001, "CheckBox": 50002, "ComboBox": 50003, "Edit": 50004,
    "Hyperlink": 50005, "Image": 50006, "ListItem": 50007, "List": 50008, "Menu": 50009,
    "MenuBar": 50010, "MenuItem": 50011, "ProgressBar": 50012, "RadioButton": 50013,
    "ScrollBar": 50014, "Slider": 50015, "Spinner": 50016, "StatusBar": 50017, "Tab": 50018,
    "TabItem": 50019, "Text": 50020, "ToolBar": 50021, "ToolTip": 50022, "Tree": 50023,
    "TreeItem": 50024, "Custom": 50025, "Group": 50026, "Thumb": 50027, "DataGrid": 50028,
    "DataItem": 50029, "Document": 50030, "SplitButton": 50031, "Window": 50032, "Pane": 50033,
    "Header": 50034, "HeaderItem": 50035, "Table": 50036, "TitleBar": 50037, "Separator": 50038,
    "SemanticZoom": 50039, "AppBar": 50040,
}
CONTROL_NAMES = {v: k for k, v in CONTROL_TYPES.items()}
UIA_ControlTypePropertyId = 30003
UIA_NamePropertyId = 30005
UIA_AutomationIdPropertyId = 30011
UIA_IsOffscreenPropertyId = 30022
MATCH_IGNORECASE_SUBSTRING = 0x1 | 0x2  # PropertyConditionFlags_IgnoreCase | MatchSubstring
TreeScope_Descendants = 0x4


class UIAError(RuntimeError):
    pass


def _explain(exc: Exception) -> str:
    text = str(exc)
    if "-2147024891" in text or "Access is denied" in text:
        return ("Windows blocked UI Automation for this app (it runs elevated, as admin, or is protected "
                "such as a game with anti-cheat). Use a window or rect target instead.")
    if "-2147220991" in text or "0x80040201" in text:
        return "the app did not answer UI Automation in time"
    return f"UI Automation failed: {text[:200]}"


def control_type_id(name: str) -> int:
    for k, v in CONTROL_TYPES.items():
        if k.lower() == name.lower():
            return v
    raise UIAError(f"unknown control_type {name!r}; use one of: {', '.join(CONTROL_TYPES)}")


class UIA:
    """One instance per thread (COM multithreaded apartment)."""

    def __init__(self) -> None:
        import sys

        sys.coinit_flags = 0  # COINIT_MULTITHREADED; comtypes reads this when first imported
        import comtypes
        import comtypes.client

        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:  # already initialised on this thread
            pass
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U

        self.U = U
        self.uia = comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
        try:
            u2 = self.uia.QueryInterface(U.IUIAutomation2)
            u2.ConnectionTimeout = 2000
            u2.TransactionTimeout = 4000
        except Exception:  # older Windows: default timeouts
            pass
        self.walker = self.uia.ControlViewWalker

    # ------------------------------------------------------------------ helpers
    def info(self, el, with_parents: bool = False) -> dict[str, Any]:
        r = el.CurrentBoundingRectangle
        d = {
            "name": (el.CurrentName or "")[:120],
            "control_type": CONTROL_NAMES.get(el.CurrentControlType, str(el.CurrentControlType)),
            "automation_id": (el.CurrentAutomationId or "")[:80],
            "class_name": (el.CurrentClassName or "")[:80],
            "rect": [r.left, r.top, r.right - r.left, r.bottom - r.top],
            "enabled": bool(el.CurrentIsEnabled),
            "offscreen": bool(el.CurrentIsOffscreen),
            "pid": el.CurrentProcessId,
        }
        if with_parents:
            chain = []
            p = el
            for _ in range(4):
                try:
                    p = self.walker.GetParentElement(p)
                except Exception:
                    break
                if not p:
                    break
                chain.append(f"{CONTROL_NAMES.get(p.CurrentControlType, '?')}:{(p.CurrentName or '')[:40]}")
            d["parents"] = chain
        return d

    # ------------------------------------------------------------------ queries
    def element_at(self, x: int, y: int) -> dict:
        try:
            el = self.uia.ElementFromPoint(self.U.tagPOINT(int(x), int(y)))
            if not el:
                raise UIAError("no element at that point")
            return self.info(el, with_parents=True)
        except UIAError:
            raise
        except Exception as exc:
            raise UIAError(_explain(exc)) from exc

    def find(self, hwnd: int, name: str | None = None, control_type: str | None = None,
             automation_id: str | None = None, limit: int = 20) -> list[dict]:
        """Visible descendants of a window matching all given filters (name = case-insensitive substring)."""
        U = self.U
        conds = [self.uia.CreatePropertyCondition(UIA_IsOffscreenPropertyId, False)]
        if name:
            conds.append(self.uia.CreatePropertyConditionEx(UIA_NamePropertyId, name, MATCH_IGNORECASE_SUBSTRING))
        if control_type:
            conds.append(self.uia.CreatePropertyCondition(UIA_ControlTypePropertyId, control_type_id(control_type)))
        if automation_id:
            conds.append(self.uia.CreatePropertyCondition(UIA_AutomationIdPropertyId, automation_id))
        cond = conds[0]
        for c in conds[1:]:
            cond = self.uia.CreateAndCondition(cond, c)
        try:
            root = self.uia.ElementFromHandle(hwnd)
            arr = root.FindAll(U.TreeScope_Descendants, cond)
            out = []
            for i in range(min(arr.Length, 400)):
                d = self.info(arr.GetElement(i))
                if d["rect"][2] <= 0 or d["rect"][3] <= 0:
                    continue
                out.append(d)
        except UIAError:
            raise
        except Exception as exc:
            raise UIAError(_explain(exc)) from exc
        if name:  # exact (case-insensitive) matches first, then shorter names; stable keeps tree order
            q = name.lower()
            out.sort(key=lambda d: (d["name"].lower() != q, len(d["name"])))
        return out[:limit]
