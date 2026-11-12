#!/usr/bin/env bash
# Checks that the Claude Code and Codex files agree, every listed plugin exists, every skill has a name and
# description, scripts parse, and nothing that looks like a secret is committed. Needs bash and jq.
# Usage: scripts/validate.sh   (run from anywhere; exits 1 on any problem)
set -euo pipefail
cd "$(dirname "$0")/.."

fail=0
bad() { printf 'FAIL  %s\n' "$*"; fail=1; }
ok() { printf 'ok    %s\n' "$*"; }

CLAUDE_CAT=.claude-plugin/marketplace.json
CODEX_CAT=.agents/plugins/marketplace.json

for f in "$CLAUDE_CAT" "$CODEX_CAT" plugins/*/.claude-plugin/plugin.json plugins/*/.codex-plugin/plugin.json; do
  jq empty "$f" 2> /dev/null || bad "$f is not valid JSON"
done
[[ $fail == 0 ]] || exit 1

[[ $(jq -r .name "$CLAUDE_CAT") == $(jq -r .name "$CODEX_CAT") ]] || bad "the two catalogs have different names"

claude_list=$(jq -r '.plugins[].name' "$CLAUDE_CAT" | sort)
codex_list=$(jq -r '.plugins[].name' "$CODEX_CAT" | sort)
folders=$(find plugins -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
[[ "$claude_list" == "$codex_list" ]] || bad "catalogs list different plugins: claude=[$claude_list] codex=[$codex_list]"
[[ "$claude_list" == "$folders" ]] || bad "plugins/ folders [$folders] don't match the catalogs [$claude_list]"

for name in $folders; do
  dir=plugins/$name
  cm=$dir/.claude-plugin/plugin.json
  xm=$dir/.codex-plugin/plugin.json
  [[ -f $cm ]] || { bad "$name: missing $cm"; continue; }
  [[ -f $xm ]] || { bad "$name: missing $xm"; continue; }

  for field in name version description license repository; do
    [[ $(jq -r ".$field" "$cm") == $(jq -r ".$field" "$xm") ]] || bad "$name: '$field' differs between the Claude and Codex manifests"
  done
  [[ $(jq -r .name "$cm") == "$name" ]] || bad "$name: manifest name doesn't match its folder"
  [[ $(jq -r .version "$cm") =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || bad "$name: version is not x.y.z"

  cat_version=$(jq -r --arg n "$name" '.plugins[] | select(.name == $n) | .version // empty' "$CLAUDE_CAT")
  [[ -z $cat_version || $cat_version == $(jq -r .version "$cm") ]] || bad "$name: Claude catalog says $cat_version, manifest says $(jq -r .version "$cm")"
  cat_desc=$(jq -r --arg n "$name" '.plugins[] | select(.name == $n) | .description // empty' "$CLAUDE_CAT")
  [[ -z $cat_desc || $cat_desc == $(jq -r .description "$cm") ]] || bad "$name: Claude catalog description differs from the manifest"

  for src in "$(jq -r --arg n "$name" '.plugins[] | select(.name == $n) | .source' "$CLAUDE_CAT")" \
             "$(jq -r --arg n "$name" '.plugins[] | select(.name == $n) | .source.path' "$CODEX_CAT")"; do
    [[ $src == "./$dir" ]] || bad "$name: catalog source '$src' should be ./$dir"
  done

  [[ -f $dir/README.md ]] || bad "$name: missing README.md"

  skills=$(find "$dir/skills" -mindepth 1 -maxdepth 1 -type d 2> /dev/null)
  [[ -n $skills ]] || bad "$name: no skills in $dir/skills"
  for s in $skills; do
    md=$s/SKILL.md
    [[ -f $md ]] || { bad "$s: missing SKILL.md"; continue; }
    head -1 "$md" | grep -qx -- '---' || bad "$md: no frontmatter"
    fm=$(awk 'NR==1{next} /^---$/{exit} {print}' "$md")
    [[ $(sed -n 's/^name: *//p' <<< "$fm") == $(basename "$s") ]] || bad "$md: 'name' must match its folder name"
    grep -q '^description: .\{20,\}' <<< "$fm" || bad "$md: missing or very short 'description'"
  done

  while IFS= read -r sh; do
    bash -n "$sh" || bad "$sh: bash syntax error"
  done < <(find "$dir" -name '*.sh' -type f)
  ok "$name $(jq -r .version "$cm")"
done

# Anything that looks like a committed credential.
if git ls-files -z 2> /dev/null | xargs -0 grep -nIE \
  '(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|-----BEGIN [A-Z ]*PRIVATE KEY|(api[_-]?key|secret|password|client_secret)["'"'"' ]*[:=] *["'"'"'][^"'"'"'<]{8,})' \
  -- 2> /dev/null; then
  bad "possible secret committed (lines above)"
fi

if [[ $fail == 0 ]]; then echo "All checks passed."; else echo "Validation failed."; exit 1; fi
