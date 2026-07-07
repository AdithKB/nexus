#!/usr/bin/env python3
"""
Nexus v6.0 — Unified Smart Capture Shortcut

Single shortcut, three surfaces:

  1. Share sheet (Safari, YouTube, Instagram…)
        Shortcut Input has URL → POST /capture  (full synthesis pipeline)

  2. Direct tap (home screen icon)
        Text input appears → smart routing via POST /capture_smart:
          • Starts with "RQ " → research query pipeline
          • Looks like a URL  → synthesis pipeline
          • Anything else     → fleeting note (Vault/Fleeting/)

  3. 🎙️ Record Audio (menu option; configure Action Button / Back Tap to reach here fast)
        Record → base64 → POST /capture_voice_fleeting
        Server transcribes via Groq; "RQ …" spoken prefix also triggers research pipeline

Routing notes
─────────────
  • The "RQ " prefix trick works for BOTH tap (typed) and hold (spoken), because
    /capture_smart and /capture_voice_fleeting both run _detect_intent() server-side.
  • Siri dictation (microphone button on keyboard) is available in the Ask prompt,
    so you can speak fleeting notes / research queries without recording a separate file.
  • iOS Action Button: configure it to run this shortcut.  The instant menu appears
    in < 0.3 s; tap 🎙️ to start recording.  1 extra tap is the unavoidable iOS constraint.
"""
import plistlib
import subprocess
import uuid
from pathlib import Path

RELAY_URL = "https://nexus.adithkb.dev"
VERSION = "6.5"
NAME = f"Nexus v{VERSION}"
SCRIPT_DIR = Path(__file__).parent


def uid():
    return str(uuid.uuid4()).upper()


# ── Token helpers (proven from v5.0) ─────────────────────────


def text_token(s):
    return {
        "Value": {"attachmentsByRange": {}, "string": s},
        "WFSerializationType": "WFTextTokenString",
    }


def input_url_token():
    """Share Extension URL with WFURLAggrandizement — extracts clean URL."""
    return {
        "Value": {
            "attachmentsByRange": {
                "{0, 1}": {
                    "Type": "ExtensionInput",
                    "Aggrandizements": [{"Type": "WFURLAggrandizement"}],
                }
            },
            "string": "￼",
        },
        "WFSerializationType": "WFTextTokenString",
    }


def extension_input_token():
    """Raw ExtensionInput token (no aggrandizement) — used in conditional check."""
    return {
        "Value": {
            "attachmentsByRange": {"{0, 1}": {"Type": "ExtensionInput"}},
            "string": "￼",
        },
        "WFSerializationType": "WFTextTokenString",
    }


def action_output_token(output_uuid, output_name):
    return {
        "Value": {
            "attachmentsByRange": {
                "{0, 1}": {
                    "Type": "ActionOutput",
                    "OutputName": output_name,
                    "OutputUUID": output_uuid,
                }
            },
            "string": "￼",
        },
        "WFSerializationType": "WFTextTokenString",
    }


def dict_item(key, value_token):
    return {
        "WFItemType": 0,
        "WFKey": text_token(key),
        "WFValue": value_token,
    }


# ── Action builders ──────────────────────────────────────────


def action_post_json(endpoint, json_items):
    """Native iOS JSON POST — WFHTTPBodyType:Json is the only reliable method."""
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
        "WFWorkflowActionParameters": {
            "UUID": uid(),
            "WFHTTPMethod": "POST",
            "WFHTTPBodyType": "Json",
            "WFURL": text_token(f"{RELAY_URL}{endpoint}"),
            "WFJSONValues": {
                "Value": {"WFDictionaryFieldValueItems": json_items},
                "WFSerializationType": "WFDictionaryFieldValue",
            },
            "WFShowHUD": False,
        },
    }


def action_ask(prompt, input_type="Text"):
    ask_uuid = uid()
    return ask_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.ask",
        "WFWorkflowActionParameters": {
            "UUID": ask_uuid,
            "WFAskActionPrompt": text_token(prompt),
            "WFInputType": input_type,
        },
    }


def action_record_audio():
    record_uuid = uid()
    return record_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.recordaudio",
        "WFWorkflowActionParameters": {
            "UUID": record_uuid,
            "WFRecordingCompression": "HighQuality",  # M4A / AAC
            "WFRecordingStart": "OnTap",
        },
    }


def action_base64_encode(input_uuid, input_name):
    encode_uuid = uid()
    return encode_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.base64encode",
        "WFWorkflowActionParameters": {
            "UUID": encode_uuid,
            "WFEncodeMode": "Encode",
            "WFBase64LineBreakMode": "None",
            "WFInput": action_output_token(input_uuid, input_name),
        },
    }


def action_notify(title, body=""):
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.notification",
        "WFWorkflowActionParameters": {
            "UUID": uid(),
            "WFNotificationActionTitle": text_token(title),
            "WFNotificationActionBody": text_token(body),
            "WFNotificationActionPlaySound": False,
        },
    }


# ── Structural control-flow helpers ─────────────────────────


def action_set_variable(name: str, input_token: dict):
    """Store a token into a named variable (needed before getvariable can produce a valid ActionOutput)."""
    action_uuid = uid()
    return action_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.setvariable",
        "WFWorkflowActionParameters": {
            "UUID": action_uuid,
            "WFVariableName": name,
            "WFInput": input_token,
        },
    }


def action_get_variable(name: str):
    """Get named variable — WFVariableName format (matches build_standard.py / v4.5 archive)."""
    action_uuid = uid()
    return action_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.getvariable",
        "WFWorkflowActionParameters": {
            "WFVariableName": name,
            "UUID": action_uuid,
        },
    }


def _var_token(output_uuid, output_name):
    """Reference a getvariable ActionOutput — used in conditional WFInput."""
    return {
        "Value": {
            "attachmentsByRange": {
                "{0, 1}": {
                    "OutputUUID": output_uuid,
                    "OutputName": output_name,
                    "Type": "ActionOutput",
                }
            },
            "string": "￼",
        },
        "WFSerializationType": "WFTextTokenString",
    }


def if_block(group_id, input_uuid, condition):
    """If conditional — params inside WFWorkflowActionParameters, NO UUID (matches build_standard.py)."""
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
        "WFWorkflowActionParameters": {
            "WFInput": _var_token(input_uuid, "Variable"),
            "WFCondition": condition,
            "GroupingIdentifier": group_id,
            "WFControlFlowMode": 0,
        },
    }


def end_if(group_id):
    """EndIf — params at ROOT LEVEL with UUID (matches build_standard.py / v4.5 archive)."""
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
        "WFControlFlowMode": 2,
        "GroupingIdentifier": group_id,
        "UUID": uid(),
    }


def begin_menu(group_id, prompt, items):
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.choosefrommenu",
        "WFWorkflowActionParameters": {
            "UUID": uid(),
            "WFControlFlowMode": 0,      # Begin
            "GroupingIdentifier": group_id,
            "WFMenuPrompt": text_token(prompt),
            "WFMenuItems": items,
        },
    }


def menu_case(group_id, label):
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.choosefrommenu",
        "WFWorkflowActionParameters": {
            "UUID": uid(),
            "WFControlFlowMode": 1,      # Case
            "WFMenuItemTitle": label,
            "GroupingIdentifier": group_id,
        },
    }


def end_menu(group_id):
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.choosefrommenu",
        "WFWorkflowActionParameters": {
            "UUID": uid(),
            "WFControlFlowMode": 2,      # End
            "GroupingIdentifier": group_id,
        },
    }


# ── Shortcut builder ─────────────────────────────────────────


def build():
    actions = []

    # Pure share-sheet capture — no conditional, no branching.
    # POST the URL straight to /capture and notify.
    actions.append(
        action_post_json(
            "/capture",
            [
                dict_item("url", input_url_token()),
                dict_item("return_files", text_token("true")),
            ],
        )
    )
    actions.append(action_notify("Nexus 🌱", "URL queued for crystallization"))

    return {
        "WFWorkflowClientVersion": "3300.0.4",
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowName": NAME,
        "WFWorkflowInputContentItemClasses": [
            "WFURLContentItem",
            "WFStringContentItem",
            "WFFileContentItem",
        ],
        "WFWorkflowTypes": ["ActionExtension"],
        "WFWorkflowActions": actions,
    }


if __name__ == "__main__":
    shortcut = build()
    unsigned = SCRIPT_DIR / f"Nexus_v{VERSION}_unsigned.shortcut"
    signed   = SCRIPT_DIR / f"Nexus_v{VERSION}_signed.shortcut"

    with open(unsigned, "wb") as f:
        plistlib.dump(shortcut, f, fmt=plistlib.FMT_BINARY)

    result = subprocess.run(
        ["/usr/bin/shortcuts", "sign", "--mode", "anyone",
         "--input", str(unsigned), "--output", str(signed)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"✓  Signed shortcut:  {signed}")
        subprocess.run(["open", str(signed)])  # adds to macOS Shortcuts → iCloud syncs to iPhone
        print(f"✓  Opened in Shortcuts app (will sync to iPhone via iCloud)")
        print()
        print("   Surfaces")
        print("   ──────────────────────────────────────────────────────")
        print("   Share sheet  →  automatic URL capture (tap Share → Nexus)")
        print("   Home screen  →  instant 2-option menu (📝 or 🎙️)")
        print("   Action Button→  configure to run this shortcut, then tap 🎙️")
    else:
        print(f"Sign failed: {result.stderr}")
        print(f"  Unsigned: {unsigned}")
