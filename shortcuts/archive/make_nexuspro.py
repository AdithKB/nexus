#!/usr/bin/env python3
"""
Generate NexusPro.shortcut — binary plist for iOS Shortcuts.
v12: Definitive ground-truth reconstruction.
"""
import plistlib
import uuid
from pathlib import Path

def uid():
    return str(uuid.uuid4()).upper()

# Pre-allocate stable identifiers
MENU_GROUP = uid()

# ── Ground Truth Helpers ────────────────────────────────────────

def text_token(s):
    """Plain text token as seen in working SendToNexus."""
    return {
        'Value': {
            'attachmentsByRange': {},
            'string': s
        },
        'WFSerializationType': 'WFTextTokenString'
    }

def input_url_token():
    """Exact 'Shortcut Input' token with URL aggrandizement."""
    return {
        'Value': {
            'attachmentsByRange': {
                '{0, 1}': {
                    'Type': 'ExtensionInput',
                    'Aggrandizements': [{'Type': 'WFURLAggrandizement'}]
                }
            },
            'string': '\ufffc'
        },
        'WFSerializationType': 'WFTextTokenString'
    }

def var_token(var_name):
    """Reference a named variable."""
    return {
        'Value': {
            'attachmentsByRange': {
                '{0, 1}': {
                    'Type': 'Variable',
                    'VariableName': var_name
                }
            },
            'string': '\ufffc'
        },
        'WFSerializationType': 'WFTextTokenString'
    }

def dict_item(key, value_token):
    """One key-value pair in a JSON body list."""
    return {
        'WFItemType': 0, # Integer
        'WFKey': text_token(key),
        'WFValue': value_token
    }

# ── Menu logic ──────────────────────────────────────────────────

def post_action(intent_value):
    """The POST call customized for a specific intent."""
    return {
        'WFWorkflowActionIdentifier': 'is.workflow.actions.downloadurl',
        'WFWorkflowActionParameters': {
            'UUID': uid(),
            'WFHTTPMethod': 'POST',
            'WFHTTPBodyType': 'Json',
            'WFURL': text_token('https://nexus.adithkb.dev/capture'),
            'WFJSONValues': {
                'Value': {
                    'WFDictionaryFieldValueItems': [
                        dict_item('url',          input_url_token()),
                        dict_item('return_files', text_token('true')),
                        dict_item('user_intent',  text_token(intent_value))
                    ]
                },
                'WFSerializationType': 'WFDictionaryFieldValue'
            },
            'WFShowHUD': False
        }
    }

def menu_branch(label, intent_value):
    """A menu branch that directly performs the POST."""
    return [
        # 1. Case Header
        {
            'WFWorkflowActionIdentifier': 'is.workflow.actions.choosefrommenu',
            'WFWorkflowActionParameters': {
                'WFControlFlowMode': 1,
                'WFMenuItemTitle': label,
                'GroupingIdentifier': MENU_GROUP
            }
        },
        # 2. POST directly inside the branch
        post_action(intent_value)
    ]

# ── Build actions ──────────────────────────────────────────────

actions = []

# Action 0: Start Menu
actions.append({
    'WFWorkflowActionIdentifier': 'is.workflow.actions.choosefrommenu',
    'WFWorkflowActionParameters': {
        'WFControlFlowMode': 0,
        'GroupingIdentifier': MENU_GROUP,
        'WFMenuPrompt': 'Select Capture Intent:',
        'WFMenuItems': [
            '🤖 AI / Tech',
            '📍 Travel',
            '🛠️ Guide',
            '📋 Ordered List',
            '🌐 General',
            '✍️ Custom...',
        ],
    }
})

# Actions 1-10: Quick Categories (1 Tap)
for label, val in [
    ('🤖 AI / Tech', 'ai'),
    ('📍 Travel',    'travel'),
    ('🛠️ Guide',     'guide'),
    ('📋 Ordered List', 'list'),
    ('🌐 General',   ''),
]:
    actions.extend(menu_branch(label, val))

# Action 11: 'Custom...' Branch with text input
actions.append({
    'WFWorkflowActionIdentifier': 'is.workflow.actions.choosefrommenu',
    'WFWorkflowActionParameters': {
        'WFControlFlowMode': 1,
        'WFMenuItemTitle': '✍️ Custom...',
        'GroupingIdentifier': MENU_GROUP
    }
})

# Ask for text
custom_ask_uuid = uid()
actions.append({
    'WFWorkflowActionIdentifier': 'is.workflow.actions.ask',
    'WFWorkflowActionParameters': {
        'UUID': custom_ask_uuid,
        'WFAskActionPrompt': 'Custom Instructions:',
        'WFInputType': 'Text'
    }
})

# POST the Custom branch
actions.append({
    'WFWorkflowActionIdentifier': 'is.workflow.actions.downloadurl',
    'WFWorkflowActionParameters': {
        'UUID': uid(),
        'WFHTTPMethod': 'POST',
        'WFHTTPBodyType': 'Json',
        'WFURL': text_token('https://nexus.adithkb.dev/capture'),
        'WFJSONValues': {
            'Value': {
                'WFDictionaryFieldValueItems': [
                    dict_item('url',          input_url_token()),
                    dict_item('return_files', text_token('true')),
                    {
                        'WFItemType': 0,
                        'WFKey': text_token('user_intent'),
                        'WFValue': {
                            'Value': {
                                'attachmentsByRange': {
                                    '{0, 1}': {
                                        'Type': 'ActionOutput',
                                        'OutputName': 'Provided Input',
                                        'OutputUUID': custom_ask_uuid
                                    }
                                },
                                'string': '\ufffc'
                            },
                            'WFSerializationType': 'WFTextTokenString'
                        }
                    }
                ]
            },
            'WFSerializationType': 'WFDictionaryFieldValue'
        },
        'WFShowHUD': False
    }
})

# Action 12: End Menu
actions.append({
    'WFWorkflowActionIdentifier': 'is.workflow.actions.choosefrommenu',
    'WFWorkflowActionParameters': {
        'WFControlFlowMode': 2,
        'GroupingIdentifier': MENU_GROUP
    }
})

# Action 12: Single Notification at the end
actions.append({
    'WFWorkflowActionIdentifier': 'is.workflow.actions.notification',
    'WFWorkflowActionParameters': {
        'WFNotificationActionTitle': text_token('Nexus Pro'),
        'WFNotificationActionBody': text_token('🌱 Seedling saved. Mac will crystallize when active.')
    }
})

# ── Final Plist ────────────────────────────────────────────────

shortcut_doc = {
    'WFWorkflowClientVersion': '1300.0.0',
    'WFWorkflowMinimumClientVersion': '900',
    'WFWorkflowName': 'Nexus Pro',
    'WFWorkflowHasShortcutInputVariables': True,
    'WFWorkflowInputContentItemClasses': ['WFURLContentItem'],
    'WFWorkflowTypes': ['ActionExtension'],
    'WFWorkflowActions': actions,
}

out = Path('/Users/adithkb/MSAI/PersonalProjects/AgenticPKM/AgenticPKMApp/shortcuts/NexusPro.shortcut')
with out.open('wb') as fh:
    plistlib.dump(shortcut_doc, fh, fmt=plistlib.FMT_BINARY)

print(f"✓ Written v12: {out}")
print(f"  Actions: {len(actions)}")
