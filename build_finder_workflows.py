#!/usr/bin/env python3
"""
生成 Finder 右键「服务」工作流（.workflow）+ 配套 .app。

macOS 26 实测：
  - 单独「运行 Shell 脚本」→ 不执行（无任何日志）
  - 单独「开启应用程序」→ 能执行，但拿不到选中文件路径
  - 组合：AppleScript 把访达传入的文件写入缓存 → .app 读缓存执行
"""

from __future__ import annotations

import os
import plistlib
import stat
import uuid
from pathlib import Path

LAUNCH_APP_ACTION = "/System/Library/Automator/Launch Application.action"
RUN_APPLESCRIPT_ACTION = "/System/Library/Automator/Run AppleScript.action"
WORKFLOW_INPUT_UUID = "00000000-0000-4000-A000-000000000002"

# 把访达传入的选中文件路径写入缓存，供后续 .app 读取
# 用 shell printf 逐行写入，避免 AppleScript write 在中文路径下丢换行、粘成一行
SAVE_PATHS_APPLESCRIPT = r'''on run {input, parameters}
	set ts to do shell script "date '+%Y-%m-%d %H:%M:%S'"
	do shell script "echo '======== " & ts & " applescript-save count=" & (count of input) & " ========' >> ~/Library/Logs/pdf-finder-tools.log"
	set cachePath to (POSIX path of (path to home folder)) & "Library/Caches/pdf-finder-selection.txt"
	do shell script "rm -f " & quoted form of cachePath
	repeat with anItem in input
		set p to ""
		try
			set p to POSIX path of (anItem as alias)
		on error
			try
				set p to anItem as text
			end try
		end try
		if p is not "" then
			do shell script "printf '%s\\n' " & quoted form of p & " >> " & quoted form of cachePath
		end if
	end repeat
	do shell script "sync; echo 'applescript saved to cache' >> ~/Library/Logs/pdf-finder-tools.log"
	return input
end run
'''


def _uid() -> str:
    return str(uuid.uuid4()).upper()


def write_runner_app(apps_dir: Path, title: str, subcmd: str, bundle_id: str) -> Path:
    """无界面 .app：读取缓存中的路径，调用 finder_service.sh。"""
    app_dir = apps_dir / f"{title}.app"
    macos_dir = app_dir / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)
    runner = macos_dir / "run"
    runner.write_text(
        f"""#!/bin/bash
LOG="$HOME/Library/Logs/pdf-finder-tools.log"
echo "======== $(date '+%Y-%m-%d %H:%M:%S') service {subcmd} app-start ========" >> "$LOG"
ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
exec bash "$ROOT/finder_service.sh" {subcmd}
""",
        encoding="utf-8",
    )
    os.chmod(runner, runner.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    info = {
        "CFBundleExecutable": "run",
        "CFBundleIdentifier": bundle_id,
        "CFBundleName": title,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "11.0",
        "LSUIElement": True,
    }
    with (app_dir / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump(info, f)
    return app_dir.resolve()


def _applescript_action() -> dict:
    """第一步：接收访达选中文件，写入缓存。"""
    action_uuid = _uid()
    return {
        "action": {
            "ActionBundlePath": RUN_APPLESCRIPT_ACTION,
            "ActionName": "运行 AppleScript",
            "ActionParameters": {"source": SAVE_PATHS_APPLESCRIPT},
            "AMAccepts": {
                "Container": "List",
                "Optional": False,
                "Types": [
                    "com.apple.cocoa.path",
                    "com.apple.cocoa.string",
                    "com.apple.alias-record",
                    "com.apple.applescript.object",
                ],
            },
            "AMActionVersion": "1.0.2",
            "AMApplication": ["Automator"],
            "AMParameterProperties": {
                "source": {"string": SAVE_PATHS_APPLESCRIPT},
            },
            "AMProvides": {
                "Container": "List",
                "Types": ["com.apple.applescript.object"],
            },
            "BundleIdentifier": "com.apple.Automator.RunScript",
            "CanShowSelectedItemsWhenRun": False,
            "CanShowWhenRun": True,
            "Category": ["AMCategoryUtilities"],
            "Class Name": "RunScriptAction",
            "InputUUID": WORKFLOW_INPUT_UUID,
            "OutputUUID": _uid(),
            "UUID": action_uuid,
            "UnlocalizedApplications": ["Automator"],
        },
        "isViewVisible": 1,
    }


def _launch_app_action(app_path: Path, after_uuid: str) -> dict:
    """第二步：启动 .app（在 macOS 26 上比 Shell 更可靠）。"""
    action_uuid = _uid()
    app_str = str(app_path)
    return {
        "action": {
            "ActionBundlePath": LAUNCH_APP_ACTION,
            "ActionName": "开启应用程序",
            "ActionParameters": {"appPath": app_str},
            "AMAccepts": {
                "Container": "List",
                "Optional": True,
                "Types": [],
            },
            "AMActionVersion": "1.1.1",
            "AMApplication": ["Finder"],
            "AMParameterProperties": {
                "appPath": {
                    "isPathPopUp": True,
                    "variableUUIDsInMenu": [],
                }
            },
            "AMProvides": {
                "Container": "List",
                "Types": [],
            },
            "BundleIdentifier": "com.apple.Automator.OpenApplication",
            "CFBundleVersion": "1.1.1",
            "CanShowSelectedItemsWhenRun": True,
            "CanShowWhenRun": True,
            "Category": ["AMCategoryUtilities"],
            "Class Name": "AMLaunchApplicationAction",
            "IgnoresInput": True,
            "InputUUID": after_uuid,
            "OutputUUID": _uid(),
            "UUID": action_uuid,
            "UnlocalizedApplications": ["Finder"],
            "arguments": {
                "0": {
                    "default value": app_str,
                    "name": "appPath",
                    "required": "0",
                    "type": "0",
                    "uuid": "0",
                }
            },
        },
        "isViewVisible": 1,
    }


def _workflow_document(app_path: Path) -> dict:
    as_action = _applescript_action()
    as_out = as_action["action"]["OutputUUID"]
    launch = _launch_app_action(app_path, as_out)
    return {
        "AMApplicationBuild": "530",
        "AMApplicationVersion": "2.10",
        "AMDocumentVersion": "2",
        "actions": [as_action, launch],
        "connectors": {
            "0": {
                "SourceUUID": WORKFLOW_INPUT_UUID,
                "DestUUID": as_action["action"]["UUID"],
            },
            "1": {
                "SourceUUID": as_action["action"]["UUID"],
                "DestUUID": launch["action"]["UUID"],
            },
        },
        "workflowMetaData": {
            "applicationBundleID": "com.apple.finder",
            "applicationBundleIDsByPath": {
                "/System/Library/CoreServices/Finder.app": "com.apple.finder",
            },
            "applicationPath": "/System/Library/CoreServices/Finder.app",
            "applicationPaths": ["/System/Library/CoreServices/Finder.app"],
            "inputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "outputTypeIdentifier": "com.apple.Automator.nothing",
            "processesInput": True,
            "serviceApplicationBundleID": "com.apple.finder",
            "serviceApplicationPath": "/System/Library/CoreServices/Finder.app",
            "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": True,
            "presentationMode": 4,
            "useAutomaticInputType": True,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }


def _info_plist(menu_title: str, bundle_id: str) -> dict:
    return {
        "CFBundleIdentifier": bundle_id,
        "CFBundleName": menu_title,
        "CFBundleVersion": "1.0",
        "CFBundleAllowMixedLocalizations": True,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundlePackageType": "BNDL",
        "AMIsApplet": True,
        "AMWorkflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        "NSServices": [
            {
                "NSMenuItem": {"default": menu_title},
                "NSMessage": "runWorkflowAsService",
                "NSSendTypes": [
                    "NSFilenamesPboardType",
                    "public.file-url",
                ],
                "NSSendFileTypes": [
                    "com.adobe.pdf",
                    "public.pdf",
                    "public.item",
                ],
                "NSReturnTypes": [],
                "NSRequiredContext": {
                    "NSApplicationIdentifier": "com.apple.finder",
                    "NSServiceCategory": "file",
                },
                "NSUserData": "Workflow",
                "NSServiceDescription": {"default": menu_title},
            }
        ],
    }


def write_workflow(out_dir: Path, menu_title: str, bundle_id: str, app_path: Path) -> None:
    wf_dir = out_dir / f"{menu_title}.workflow"
    contents = wf_dir / "Contents"
    contents.mkdir(parents=True, exist_ok=True)
    with (contents / "document.wflow").open("wb") as f:
        plistlib.dump(_workflow_document(app_path), f)
    with (contents / "Info.plist").open("wb") as f:
        plistlib.dump(_info_plist(menu_title, bundle_id), f)


def main() -> None:
    root = Path(__file__).resolve().parent
    apps_dir = root / "finder-apps"
    out = root / "finder-services"
    apps_dir.mkdir(exist_ok=True)
    out.mkdir(exist_ok=True)

    items = [
        ("PDF 合并", "merge", "com.fuchengwang.pdf-merge.runner"),
        ("PDF 双语版", "bilingual", "com.fuchengwang.pdf-bilingual.runner"),
        ("PDF 拆页", "split", "com.fuchengwang.pdf-split.runner"),
        ("⭐️PDF翻译", "translate", "com.fuchengwang.pdf-translate.runner"),
    ]
    for title, subcmd, bid in items:
        app = write_runner_app(apps_dir, title, subcmd, bid)
        write_workflow(out, title, bid.replace(".runner", ""), app)
        print(f"  {title}")

    print(f"已生成：{out}")


if __name__ == "__main__":
    main()
