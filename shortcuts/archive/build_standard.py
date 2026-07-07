#!/usr/bin/env python3
"""
Nexus v4.5 — "The Sovereign Standard"
- Full Menu Flow (AI, Travel, Guide, Paste, Research)
- Named Variable Initialization (Fixes macOS Parameter Error)
- Forced Identity Update (Nexus v4.5)
- Silent Stealth Mode
"""
import plistlib
import subprocess
import uuid
from pathlib import Path

RELAY_URL = "https://nexus.adithkb.dev"
VERSION = "4.5"
NAME = f"Nexus v{VERSION}"


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


def action_alert(title, message_text):
    action_uuid = uid()
    return action_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.alert",
        "WFWorkflowActionParameters": {
            "WFAlertActionTitle": text_token(title),
            "WFAlertActionMessage": text_token(message_text),
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


def action_get_clipboard():
    action_uuid = uid()
    return action_uuid, {
        "WFWorkflowActionIdentifier": "is.workflow.actions.getclipboard",
        "WFWorkflowActionParameters": {"UUID": action_uuid},
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


def _add_quick_capture_branch(actions, menu_group, item_label, static_intent):
    _, mi_act = action_menu_item(item_label, menu_group)
    actions.append(mi_act)

    # 1. Resolve URL (Linear Waterfall)
    actions.append(action_set_variable("ResolvedURL", shortcut_input_token())[1])
    
    check_group = uid()
    gv_uuid, gv_act = action_get_variable("ResolvedURL")
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
    clip_uuid, clip_act = action_get_clipboard()
    actions.append(clip_act)
    actions.append(action_set_variable("ResolvedURL", var_token(clip_uuid, "Clipboard"))[1])
    actions.append({"WFWorkflowActionIdentifier": "is.workflow.actions.conditional", "WFControlFlowMode": 2, "GroupingIdentifier": check_group, "UUID": uid()})

    # 2. Final prompt if STILL empty
    check2_group = uid()
    gv2_uuid, gv2_act = action_get_variable("ResolvedURL")
    actions.append(gv2_act)
    actions.append({
        "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
        "WFWorkflowActionParameters": {
            "WFInput": var_token(gv2_uuid, "Variable"),
            "WFCondition": 101,
            "GroupingIdentifier": check2_group,
            "WFControlFlowMode": 0,
        },
    })
    ask_uuid, ask_act = action_ask_for_input("URL missing. Please paste here:", input_type="URL")
    actions.append(ask_act)
    actions.append(action_set_variable("ResolvedURL", var_token(ask_uuid, "Provided Input"))[1])
    actions.append({"WFWorkflowActionIdentifier": "is.workflow.actions.conditional", "WFControlFlowMode": 2, "GroupingIdentifier": check2_group, "UUID": uid()})

    # 3. Build Body String
    # Magic Variable usage in Text block is the most stable way
    json_body_uuid, json_body_act = action_text(
        '{"url": "\ufffc", "user_intent": "' + static_intent + '"}',
        attachments={"{9, 1}": named_var_token("ResolvedURL")}
    )
    actions.append(json_body_act)

    # 4. POST RAW
    _, dl_act = action_post_file(f"{RELAY_URL}/capture", json_body_uuid, "Text")
    actions.append(dl_act)


# ── Builder ──────────────────────────────────────────────────


def build_shortcut():
    actions = []
    
    menu_group = uid()
    _, menu_open_act = action_menu_open("Nexus Pipeline", ["🤖 AI / Tech", "📍 Travel", "🛠️ Guide", "📝 Paste Content", "🔬 Research Topic"], menu_group)
    actions.append(menu_open_act)

    # Branches 1-3: Quick Capture
    _add_quick_capture_branch(actions, menu_group, "🤖 AI / Tech", "ai")
    _add_quick_capture_branch(actions, menu_group, "📍 Travel", "travel")
    _add_quick_capture_branch(actions, menu_group, "🛠️ Guide", "guide")

    # Branch 4: Paste Content
    actions.append(action_menu_item("📝 Paste Content", menu_group)[1])
    clip_uuid, clip_act = action_get_clipboard()
    actions.append(clip_act)
    txt_uuid, txt_act = action_text('{"text": "\ufffc"}', attachments={"{10, 1}": var_token(clip_uuid, "Clipboard")})
    actions.append(txt_act)
    _, dl_act_text = action_post_file(f"{RELAY_URL}/capture_text", txt_uuid, "Text")
    actions.append(dl_act_text)

    # Branch 5: Research Topic
    actions.append(action_menu_item("🔬 Research Topic", menu_group)[1])
    topic_uuid, topic_act = action_ask_for_input("Research Topic?", input_type="Text")
    actions.append(topic_act)
    rt_uuid, rt_act = action_text('{"topic_intent": "\ufffc"}', attachments={"{18, 1}": var_token(topic_uuid, "Provided Input")})
    actions.append(rt_act)
    _, dl_act_rt = action_post_file(f"{RELAY_URL}/research_topic", rt_uuid, "Text")
    actions.append(dl_act_rt)

    # Close Menu
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
    signed = Path(f"shortcuts/Nexus_v{VERSION}_signed.shortcut")
    unsigned = Path(f"shortcuts/Nexus_v{VERSION}_unsigned.shortcut")
    
    with open(unsigned, "wb") as f:
        plistlib.dump(shortcut, f, fmt=plistlib.FMT_BINARY)
    
    subprocess.run(["/usr/bin/shortcuts", "sign", "--mode", "anyone", "--input", str(unsigned), "--output", str(signed)])
    print(f"Generated and Signed: {signed}")
    print(f"Run: open \"{signed.absolute()}\"")
