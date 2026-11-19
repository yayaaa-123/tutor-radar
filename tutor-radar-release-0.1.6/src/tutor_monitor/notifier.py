from __future__ import annotations

import base64
import subprocess
import sys
from typing import Any


def format_candidate_notification(candidates: list[dict[str, Any]]) -> tuple[str, str]:
    count = len(candidates)
    title = f"发现 {count} 个家教候选"
    lines: list[str] = []
    for item in candidates[:3]:
        order = item["order"]
        district = order.get("district") or "区域待确认"
        subjects = "/".join(order.get("subjects") or []) or "科目待确认"
        pay = order.get("pay_text") or "薪酬待确认"
        score = item.get("score") or {}
        score_text = (
            f"{score.get('recommendation')} {score.get('total_score')}分｜"
            if score.get("recommendation") and score.get("total_score") is not None
            else ""
        )
        lines.append(f"{score_text}{district}｜{subjects}｜{pay}")
    if count > 3:
        lines.append(f"另有 {count - 3} 个候选，详见本地记录")
    return title, "\n".join(lines)


def show_windows_notification(
    candidates: list[dict[str, Any]], display_seconds: int = 8
) -> bool:
    if sys.platform != "win32" or not candidates:
        return False
    title, body = format_candidate_notification(candidates)
    safe_title = title.replace("'", "''")
    safe_body = body.replace("'", "''")
    seconds = max(3, min(int(display_seconds), 30))
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$notification = New-Object System.Windows.Forms.NotifyIcon
$notification.Icon = [System.Drawing.SystemIcons]::Information
$notification.Visible = $true
$notification.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
$notification.BalloonTipTitle = '{safe_title}'
$notification.BalloonTipText = '{safe_body}'
$notification.ShowBalloonTip({seconds * 1000})
Start-Sleep -Seconds {seconds}
$notification.Dispose()
"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    return True
