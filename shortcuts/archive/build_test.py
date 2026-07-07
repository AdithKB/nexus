#!/usr/bin/env python3
"""
CAPTURE_TO_NEXUS — Minimalist Test Shortcut
- No menus, no branches.
- Just Clipboard -> POST.
- Designed to bypass macOS Sequoia caching issues.
"""
import plistlib
import subprocess
import uuid
from pathlib import Path

RELAY_URL = "https://nexus.adithkb.dev"
NAME = "CAPTURE_TO_NEXUS"


def uid():
    return str(uuid.uuid4()).upper()


def text_token(s):
    return {
        "Value": {"attachmentsByRange": {}, "string": s},
        "WFSerializationType": "WFTextTokenString",
    }


def var_ref(output_uuid, output_name):
    return {"OutputUUID": output_uuid, "OutputName": output_name, "Type": "ActionOutput"}


def var_token(output_uuid, output_name):
    return {
        "Value": {
            "attachmentsByRange": {"{0, 1}": var_ref(output_uuid, output_name)},
            "string": "\ufffc",
        },
        "WFSerializationType": "WFTextTokenString",
    }


def build_shortcut():
    actions = []
    
    # 1. Alert: Starting
    actions.append({
        "WFWorkflowActionIdentifier": "is.workflow.actions.alert",
        "WFWorkflowActionParameters": {
            "WFAlertActionTitle": text_token("Nexus Test"),
            "WFAlertActionMessage": text_token("Reading clipboard..."),
            "WFAlertActionCancelButtonShown": False,
            "UUID": uid(),
        },
    })

    # 2. Get Clipboard
    clip_uuid = uid()
    actions.append({
        "WFWorkflowActionIdentifier": "is.workflow.actions.getclipboard",
        "WFWorkflowActionParameters": {"UUID": clip_uuid},
    })

    # 3. Alert: Show Payload
    actions.append({
        "WFWorkflowActionIdentifier": "is.workflow.actions.alert",
        "WFWorkflowActionParameters": {
            "WFAlertActionTitle": text_token("Nexus Payload Check"),
            "WFAlertActionMessage": var_token(clip_uuid, "Clipboard"),
            "WFAlertActionCancelButtonShown": False,
            "UUID": uid(),
        },
    })

    # 4. Build JSON String manually
    json_uuid = uid()
    actions.append({
        "WFWorkflowActionIdentifier": "is.workflow.actions.gettext",
        "WFWorkflowActionParameters": {
            "WFTextActionText": {
                "Value": {
                    "attachmentsByRange": {"{9, 1}": var_ref(clip_uuid, "Clipboard")},
                    "string": '{"url": "\ufffc", "user_intent": "ai"}'
                },
                "WFSerializationType": "WFTextTokenString"
            },
            "UUID": json_uuid,
        },
    })

    # 5. POST as File
    actions.append({
        "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
        "WFWorkflowActionParameters": {
            "WFURL": text_token(f"{RELAY_URL}/capture"),
            "WFHTTPMethod": "POST",
            "WFHTTPBodyType": "File",
            "WFFile": var_ref(json_uuid, "Text"),
            "UUID": uid(),
        },
    })

    # 6. Final Alert
    actions.append({
        "WFWorkflowActionIdentifier": "is.workflow.actions.alert",
        "WFWorkflowActionParameters": {
            "WFAlertActionTitle": text_token("Nexus"),
            "WFAlertActionMessage": text_token("Sent successfully!"),
            "WFAlertActionCancelButtonShown": False,
            "UUID": uid(),
        },
    })

    return {
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowClientVersion": "3300.0.4",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4282601983, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowTypes": ["NCWidget"],
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem"],
        "WFWorkflowActions": actions,
        "WFWorkflowName": NAME,
    }


if __name__ == "__main__":
    shortcut = build_shortcut()
    unsigned = Path("shortcuts/CAPTURE_TEST_unsigned.shortcut")
    signed   = Path("shortcuts/CAPTURE_TEST_signed.shortcut")
    with open(unsigned, "wb") as f:
        plistlib.dump(shortcut, f, fmt=plistlib.FMT_BINARY)
    subprocess.run(["/usr/bin/shortcuts", "sign", "--mode", "anyone", "--input", str(unsigned), "--output", str(signed)])
    print(f"Generated: {signed}")
    print(f"Run: open \"{signed.absolute()}\"")
