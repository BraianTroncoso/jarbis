#!/bin/bash
# Generate codebase.txt — a text dump of the entire codebase.

OUTPUT=".myjarbis/context/codebase.txt"
echo "Generating codebase context..."
echo "# Codebase Context - Generated $(date)" > "$OUTPUT"
echo "" >> "$OUTPUT"

find . -type f \
  -not -path "*/node_modules/*" \
  -not -path "*/vendor/*" \
  -not -path "*/.git/*" \
  -not -path "*/build/*" \
  -not -path "*/dist/*" \
  -not -path "*/.myjarbis/*" \
  -not -name "*.min.js" \
  -not -name "package-lock.json" \
  | while read file; do
    echo "--- $file" >> "$OUTPUT"
    cat "$file" >> "$OUTPUT"
    echo "" >> "$OUTPUT"
done

echo "Generated: $OUTPUT"
