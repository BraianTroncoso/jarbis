#!/bin/bash
# Update daily.md with current date.
DAILY_FILE=".myjarbis/context/daily.md"
TODAY=$(date +%Y-%m-%d)
sed -i.bak "1s/.*/# Daily Context - $TODAY/" "$DAILY_FILE"
sed -i.bak "s/Last updated: .*/Last updated: $TODAY/" "$DAILY_FILE"
rm -f "${DAILY_FILE}.bak"
echo "Updated daily.md"
