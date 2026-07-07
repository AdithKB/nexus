#!/usr/bin/env python3
"""
Nexus v5.0 — "The Reliable One"

Fixes the broken v4.3.8–v4.5 shortcut chain.

Root cause of failure: v4.3.8+ switched from WFHTTPBodyType:'Json' to 'File'
(raw text string body). The 'File' approach broke when variable references
didn't resolve correctly — resulting in empty bodies sent to the server.

Fix: Back to WFHTTPBodyType:'Json' with WFJSONValues (native iOS dict), which
is what make_nexuspro.py used and what worked reliably through March 28.

Also uses input_url_token() WITH WFURLAggrandizement (exact URL extraction
from the share sheet), not a raw ExtensionInput token.
"""
import plistlib
import subprocess
import uuid
from pathlib import Path

RELAY_URL = "https://nexus.adithkb.dev"
VERSION = "5.0"
NAME = f"Nexus v{VERSION}"
SCRIPT_DIR = Path(__file__).parent


def uid():
    return str(uuid.uuid4()).upper()


# ── Token helpers (proven from make_nexuspro.py) ─────────────


def text_token(s):
    return {
        "Value": {"attachmentsByRange": {}, "string": s},
        "WFSerializationType": "WFTextTokenString",
    }


def input_url_token():
    """Share Extension URL — WITH WFURLAggrandizement to extract clean URL."""
    return {
        "Value": {
            "attachmentsByRange": {
                "{0, 1}": {
                    "Type": "ExtensionInput",
                    "Aggrandizements": [{"Type": "WFURLAggrandizement"}],
                }
            },
            "string": "\ufffc",
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
            "string": "\ufffc",
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
    """Native iOS JSON POST — the only reliable body type."""
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
        "WFWorkflowActionParameters": {
            "UUID": uid(),
            "WFHTTPMethod": "POST",
            "WFHTTPBodyType": "Json",
            "WFURL": text_token(f"{RELAY_URL}{endpoint}"),
            "WFJSONValues": {
                "Value": {
                    "WFDictionaryFieldValueItems": json_items,
                },
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


def action_notify(title, body):
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.notification",
        "WFWorkflowActionParameters": {
            "WFNotificationActionTitle": text_token(title),
            "WFNotificationActionBody": text_token(body),
        },
    }


# ── Capture branch ───────────────────────────────────────────


def capture_branch(menu_group, label, static_intent):
    """
    A single menu branch that POSTs to /capture with Json body.
    Uses input_url_token() directly — most reliable for share sheet.
    """
    return [
        # Menu case header
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.choosefrommenu",
            "WFWorkflowActionParameters": {
                "WFControlFlowMode": 1,
                "WFMenuItemTitle": label,
                "GroupingIdentifier": menu_group,
            },
        },
        # POST directly — input_url_token() resolves the shared URL
        action_post_json(
            "/capture",
            [
                dict_item("url", input_url_token()),
                dict_item("return_files", text_token("true")),
                dict_item("user_intent", text_token(static_intent)),
            ],
        ),
    ]


# ── Builder ──────────────────────────────────────────────────


def build():
    menu_group = uid()
    actions = []

    # Menu
    actions.append({
        "WFWorkflowActionIdentifier": "is.workflow.actions.choosefrommenu",
        "WFWorkflowActionParameters": {
            "WFControlFlowMode": 0,
            "GroupingIdentifier": menu_group,
            "WFMenuPrompt": text_token("Nexus — Select Intent"),
            "WFMenuItems": [
                "🤖 AI / Tech",
                "📍 Travel",
                "🛠️ Guide",
                "🌐 General",
                "📝 Paste Text",
                "🔬 Research Topic",
            ],
        },
    })

    # Quick capture branches
    for label, intent in [
        ("🤖 AI / Tech", "ai"),
        ("📍 Travel", "travel"),
        ("🛠️ Guide", "guide"),
        ("🌐 General", ""),
    ]:
        actions.extend(capture_branch(menu_group, label, intent))

    # Paste Text branch
    actions.append({
        "WFWorkflowActionIdentifier": "is.workflow.actions.choosefrommenu",
        "WFWorkflowActionParameters": {
            "WFControlFlowMode": 1,
            "WFMenuItemTitle": "📝 Paste Text",
            "GroupingIdentifier": menu_group,
        },
    })
    ask_uuid, ask_act = action_ask("Paste or type content:")
    actions.append(ask_act)
    actions.append(
        action_post_json(
            "/capture_text",
            [dict_item("text", action_output_token(ask_uuid, "Provided Input"))],
        )
    )

    # Research Topic branch
    actions.append({
        "WFWorkflowActionIdentifier": "is.workflow.actions.choosefrommenu",
        "WFWorkflowActionParameters": {
            "WFControlFlowMode": 1,
            "WFMenuItemTitle": "🔬 Research Topic",
            "GroupingIdentifier": menu_group,
        },
    })
    topic_uuid, topic_act = action_ask("Research topic?")
    actions.append(topic_act)
    actions.append(
        action_post_json(
            "/research_topic",
            [dict_item("topic_intent", action_output_token(topic_uuid, "Provided Input"))],
        )
    )

    # End menu
    actions.append({
        "WFWorkflowActionIdentifier": "is.workflow.actions.choosefrommenu",
        "WFWorkflowActionParameters": {
            "WFControlFlowMode": 2,
            "GroupingIdentifier": menu_group,
        },
    })

    # Notification
    actions.append(action_notify("Nexus", "🌱 Sent — Mac will crystallize when active."))

    return {
        "WFWorkflowClientVersion": "1300.0.0",
        "WFWorkflowMinimumClientVersion": "900",
        "WFWorkflowName": NAME,
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowInputContentItemClasses": ["WFURLContentItem", "WFStringContentItem"],
        "WFWorkflowTypes": ["ActionExtension"],
        "WFWorkflowActions": actions,
    }


if __name__ == "__main__":
    shortcut = build()
    unsigned = SCRIPT_DIR / f"Nexus_v{VERSION}_unsigned.shortcut"
    signed = SCRIPT_DIR / f"Nexus_v{VERSION}_signed.shortcut"

    with open(unsigned, "wb") as f:
        plistlib.dump(shortcut, f, fmt=plistlib.FMT_BINARY)

    result = subprocess.run(
        ["/usr/bin/shortcuts", "sign", "--mode", "anyone",
         "--input", str(unsigned), "--output", str(signed)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"✓ Signed: {signed}")
        print(f'  Install: open "{signed.absolute()}"')
    else:
        print(f"Sign failed: {result.stderr}")
        print(f"  Unsigned: {unsigned}")
