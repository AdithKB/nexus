#!/usr/bin/env python3
"""
NexusPro v4.4.5 — "The Diagnostic Bridge"
- Named Variables for maximum stability
- Hard Validation Stops
- Pre-flight Alert to confirm URL capture
"""
import plistlib
import subprocess
import uuid
from pathlib import Path

RELAY_URL = "https://nexus.adithkb.dev"
VERSION = "4.4.5"
NAME = f"NexusPro v{VERSION}"


def uid():
    return str(uuid.uuid4()).upper()


def text_token(s):
    return {
        "Value": {"attachmentsByRange": {}, "string": s},
        "WFSerializationType": "WFTextTokenString",
    }


def var_ref(output_uuid, output_name):
    return {"OutputUUID": output_uuid, "OutputName": output_name, "Type": "ActionOutput"}


def shortcut_input_token():
    return {
        "Value": {
            "attachmentsByRange": {"{0, 1}": {"Type": "ExtensionInput"}},
            "string": "\ufffc",
        },
        "WFSerializationType": "WFTextTokenString",
    }


def var_token(output_uuid, output_name):
    return {
        "Value": {
            "attachmentsByRange": {"{0, 1}": var_ref(output_uuid, output_name)},
            "string": "\ufffc",
        },
        "WFSerializationType": "WFTextTokenString",
    }


def named_var_token(name):
    return {
        "Value": {
            "attachmentsByRange": {
                "{0, 1}": {"PropertyName": name, "Type": "Variable", "VariableName": name}
            },
            "string": "\ufffc",
        },
        "WFSerializationType": "WFTextTokenString",
    }


# ── Action builders ──────────────────────────────────────────


def action_alert(title, message_token):
    action_uuid = uid()
    return action_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.alert",
        "WFWorkflowActionParameters": {
            "WFAlertActionTitle": text_token(title),
            "WFAlertActionMessage": message_token,
            "WFAlertActionCancelButtonShown": False,
            "UUID": action_uuid,
        },
    }


def action_set_variable(name, input_token):
    action_uuid = uid()
    return action_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.setvariable",
        "WFWorkflowActionParameters": {
            "WFVariableName": name,
            "WFInput": input_token,
            "UUID": action_uuid,
        },
    }


def action_get_variable(name):
    action_uuid = uid()
    return action_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.getvariable",
        "WFWorkflowActionParameters": {
            "WFVariableName": name,
            "UUID": action_uuid,
        },
    }


def action_ask_for_input(prompt, input_type="Text"):
    action_uuid = uid()
    return action_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.ask",
        "WFWorkflowActionParameters": {
            "WFAskActionPrompt": text_token(prompt),
            "WFInputType": input_type,
            "UUID": action_uuid,
        },
    }


def action_stop(message):
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.exit",
        "WFWorkflowActionParameters": {"WFNoOutput": True, "WFAlertActionMessage": text_token(message)},
    }


def action_text(text_string, attachments=None):
    action_uuid = uid()
    return action_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.gettext",
        "WFWorkflowActionParameters": {
            "WFTextActionText": {
                "Value": {
                    "attachmentsByRange": attachments or {},
                    "string": text_string
                },
                "WFSerializationType": "WFTextTokenString"
            },
            "UUID": action_uuid,
        },
    }


def action_post_file(url_string, body_uuid, body_name):
    action_uuid = uid()
    return action_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
        "WFWorkflowActionParameters": {
            "WFURL": text_token(url_string),
            "WFHTTPMethod": "POST",
            "WFHTTPBodyType": "File",
            "WFFile": var_ref(body_uuid, body_name),
            "UUID": action_uuid,
        },
    }


def action_menu_open(prompt, items, group_id):
    action_uuid = uid()
    return action_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.choosefrommenu",
        "WFWorkflowActionParameters": {
            "WFMenuPrompt": text_token(prompt),
            "WFMenuItems": items,
            "GroupingIdentifier": group_id,
            "WFControlFlowMode": 0,
            "UUID": action_uuid,
        },
    }


def action_menu_item(title, group_id):
    action_uuid = uid()
    return action_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.choosefrommenu",
        "WFWorkflowActionParameters": {
            "WFMenuItemTitle": title,
            "GroupingIdentifier": group_id,
            "WFControlFlowMode": 1,
            "UUID": action_uuid,
        },
    }


def action_menu_close(group_id):
    action_uuid = uid()
    return action_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.choosefrommenu",
        "WFWorkflowActionParameters": {
            "GroupingIdentifier": group_id,
            "WFControlFlowMode": 2,
            "UUID": action_uuid,
        },
    }


# ── Branch helpers ───────────────────────────────────────────


def _add_quick_capture_branch(actions, menu_group, item_label, static_intent=None):
    _, mi_act = action_menu_item(item_label, menu_group)
    actions.append(mi_act)

    # 1. Resolve URL
    actions.append(action_set_variable("FinalURL", shortcut_input_token())[1])
    
    check_group = uid()
    gv_uuid, gv_act = action_get_variable("FinalURL")
    actions.append(gv_act)
    actions.append({
        "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
        "WFWorkflowActionParameters": {
            "WFInput": var_token(gv_uuid, "Variable"),
            "WFCondition": 101, # Does Not Have Any Value
            "GroupingIdentifier": check_group,
            "WFControlFlowMode": 0,
        },
    })
    ask_uuid, ask_act = action_ask_for_input("Paste URL:", input_type="Text")
    actions.append(ask_act)
    actions.append(action_set_variable("FinalURL", var_token(ask_uuid, "Provided Input"))[1])
    actions.append({"WFWorkflowActionIdentifier": "is.workflow.actions.conditional", "WFControlFlowMode": 2, "GroupingIdentifier": check_group, "UUID": uid()})

    # 2. Pre-flight Alert
    actions.append(action_alert("Nexus Diagnostic", named_var_token("FinalURL"))[1])

    # 3. Intent
    if static_intent is None:
        intent_uuid, intent_act = action_ask_for_input("Intent?")
        actions.append(intent_act)
        intent_token_val = var_token(intent_uuid, "Provided Input")
    else:
        intent_token_val = text_token(static_intent)

    # 4. Build JSON String
    json_uuid, json_act = action_text(
        '{"url": "\ufffc", "user_intent": "\ufffc"}',
        attachments={
            "{9, 1}": named_var_token("FinalURL"),
            "{29, 1}": intent_token_val
        }
    )
    actions.append(json_act)

    # 5. POST
    _, dl_act = action_post_file(f"{RELAY_URL}/capture", json_uuid, "Text")
    actions.append(dl_act)


# ── Builder ──────────────────────────────────────────────────


def build_shortcut():
    actions = []
    
    # 0. Start Alert
    _, alert_act = action_alert("Nexus", text_token(f"Initializing {NAME}..."))
    actions.append(alert_act)

    menu_group = uid()
    _, menu_open_act = action_menu_open("Options", ["🤖 AI / Tech", "📍 Travel", "📝 Paste Content", "🔬 Research Topic"], menu_group)
    actions.append(menu_open_act)

    for label, intent in [("🤖 AI / Tech", "ai"), ("📍 Travel", "travel")]:
        _add_quick_capture_branch(actions, menu_group, label, static_intent=intent)

    # Paste Content
    actions.append(action_menu_item("📝 Paste Content", menu_group)[1])
    # For Paste Content, we use Clipboard directly
    ask_uuid, ask_act = action_ask_for_input("Content to paste:", input_type="Text") # Prompt for content to be safe
    actions.append(ask_act)
    txt_uuid, txt_act = action_text('{"text": "\ufffc"}', attachments={"{10, 1}": var_token(ask_uuid, "Provided Input")})
    actions.append(txt_act)
    _, dl_act_text = action_post_file(f"{RELAY_URL}/capture_text", txt_uuid, "Text")
    actions.append(dl_act_text)

    # Research Topic
    actions.append(action_menu_item("🔬 Research Topic", menu_group)[1])
    topic_uuid, topic_act = action_ask_for_input("Topic?", input_type="Text")
    actions.append(topic_act)
    rt_uuid, rt_act = action_text('{"topic_intent": "\ufffc"}', attachments={"{18, 1}": var_ref(topic_uuid, "Provided Input")})
    actions.append(rt_act)
    _, dl_act_rt = action_post_file(f"{RELAY_URL}/research_topic", rt_uuid, "Text")
    actions.append(dl_act_rt)

    actions.append(action_menu_close(menu_group)[1])

    return {
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowClientVersion": "3300.0.4",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4282601983, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowTypes": ["NCWidget", "ActionExtension"],
        "WFWorkflowInputContentItemClasses": ["WFURLContentItem", "WFStringContentItem"],
        "WFWorkflowActions": actions,
        "WFWorkflowName": NAME,
    }


if __name__ == "__main__":
    shortcut = build_shortcut()
    unsigned = Path(f"shortcuts/NexusPro_v{VERSION}_unsigned.shortcut")
    signed   = Path(f"shortcuts/NexusPro_v{VERSION}_signed.shortcut")
    with open(unsigned, "wb") as f:
        plistlib.dump(shortcut, f, fmt=plistlib.FMT_BINARY)
    subprocess.run(["/usr/bin/shortcuts", "sign", "--mode", "anyone", "--input", str(unsigned), "--output", str(signed)])
    print(f"Generated: {signed}")
    print(f"Run: open \"{signed.absolute()}\"")
