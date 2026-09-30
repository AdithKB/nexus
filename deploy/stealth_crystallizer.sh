#!/bin/bash
# M4 Stealth Launcher v4.0.1
# Runs the watcher (Stage 2) at Utility QoS to prevent UI lag on fanless Air.
echo "[STEALTH] Launching Nexus Watcher (v4.0 Sovereign) at Utility QoS..."
export OLLAMA_MAX_LOADED_MODELS=2
export OLLAMA_NUM_PARALLEL=1
# We use -u for unbuffered output to ensure logs are real-time
taskpolicy -c utility /opt/anaconda3/bin/python3 -u /Users/adithkb/Ireland/PersonalProjects/AgenticPKM/AgenticPKMApp/watcher.py
