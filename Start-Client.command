#!/bin/sh
cd "$HOME/Applications/PrecisionKVM" || exit 1
exec .venv/bin/python -u -m precision_kvm client --config "$HOME/.config/PrecisionKVM/client.json"
