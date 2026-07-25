#!/usr/bin/env bash
# Audits local branches: merged, unpushed commits, diverged, and unmerged dirty branches.
# Also detects stale worktrees and remote branches merged into main.
#
# Usage:
#   branch-audit.sh                          # report only
#   branch-audit.sh --clean-local            # remove stale worktrees + delete merged local branches
#   branch-audit.sh --clean-remote           # delete merged remote branches on origin
#   branch-audit.sh --clean-all              # both of the above
#   branch-audit.sh --clean-* --dry-run      # show what would be done without executing

set -euo pipefail

# MAIN/REMOTE overridable via env; MAIN falls back to the remote's HEAD branch, then "main".
REMOTE="${REMOTE:-origin}"
MAIN="${MAIN:-$(git symbolic-ref --short "refs/remotes/$REMOTE/HEAD" 2>/dev/null | sed "s|^$REMOTE/||")}"
MAIN="${MAIN:-main}"

RED='\033[0;31m'
YEL='\033[1;33m'
CYA='\033[0;36m'
GRN='\033[0;32m'
MAG='\033[0;35m'
BLD='\033[1m'
DIM='\033[2m'
RST='\033[0m'

clean_local=false
clean_remote=false
dry_run=false

for arg in "$@"; do
  case "$arg" in
    --clean-local)  clean_local=true ;;
    --clean-remote) clean_remote=true ;;
    --clean-all)    clean_local=true; clean_remote=true ;;
    --dry-run)      dry_run=true ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

# run_cmd: prints the command, executes only when not in dry-run mode
run_cmd() {
  if $dry_run; then
    echo -e "    ${DIM}[dry-run]${RST} $*"
  else
    echo -e "    $*"
    "$@"
  fi
}

# Fetch quietly to get up-to-date remote state
git fetch --prune "$REMOTE" 2>/dev/null

main_tip=$(git rev-parse "$MAIN")
current=$(git rev-parse --abbrev-ref HEAD)

# ── Build worktree index: "branch\tpath" lines (bash 3.2 compatible) ─────────
worktree_index=""
while IFS= read -r line; do
  wt_path=$(echo "$line" | awk '{print $1}')
  wt_branch=$(echo "$line" | sed -n 's/.*\[\(.*\)\].*/\1/p')
  [[ -n "$wt_branch" ]] && worktree_index+="${wt_branch}	${wt_path}"$'\n'
done < <(git worktree list)

worktree_for() {
  echo "$worktree_index" | awk -F'\t' -v b="$1" '$1==b{print $2; exit}'
}

# ── Classify local branches ───────────────────────────────────────────────────
merged_into_main=()   # "branch|worktree_path_or_empty"
dirty=()              # "branch|remote_status|ahead_main"
unpushed=()           # "branch|push_status|ahead_main"
diverged=()           # "branch|divergence|ahead_main"

while IFS= read -r branch; do
  [[ "$branch" == "$MAIN" ]] && continue
  [[ "$branch" == "$current" ]] && continue

  remote_ref="$REMOTE/$branch"
  has_remote=false
  git rev-parse --verify "$remote_ref" &>/dev/null && has_remote=true

  branch_tip=$(git rev-parse "$branch")

  if git merge-base --is-ancestor "$branch_tip" "$main_tip" 2>/dev/null; then
    wt=$(worktree_for "$branch")
    merged_into_main+=("$branch|$wt")
    continue
  fi

  ahead_main=$(git rev-list --count "$MAIN".."$branch" 2>/dev/null || echo 0)

  if $has_remote; then
    ahead_remote=$(git rev-list --count "$remote_ref".."$branch" 2>/dev/null || echo 0)
    behind_remote=$(git rev-list --count "$branch".."$remote_ref" 2>/dev/null || echo 0)

    if [[ "$ahead_remote" -gt 0 && "$behind_remote" -gt 0 ]]; then
      diverged+=("$branch|+${ahead_remote}/-${behind_remote} vs remote|+${ahead_main} vs main")
    elif [[ "$ahead_remote" -gt 0 ]]; then
      unpushed+=("$branch|${ahead_remote} unpushed|+${ahead_main} vs main")
    else
      dirty+=("$branch|in sync with remote|+${ahead_main} vs main")
    fi
  else
    unpushed+=("$branch|no remote branch|+${ahead_main} vs main")
  fi

done < <(git branch --format='%(refname:short)')

# ── Remote branches merged into main ─────────────────────────────────────────
remote_merged=()
while IFS= read -r ref; do
  # Only process refs that genuinely start with "$REMOTE/" to avoid symrefs
  [[ "$ref" != "$REMOTE/"* ]] && continue
  branch="${ref#$REMOTE/}"
  [[ "$branch" == "HEAD" || "$branch" == "$MAIN" ]] && continue

  remote_tip=$(git rev-parse "$ref" 2>/dev/null || true)
  [[ -z "$remote_tip" ]] && continue

  if git merge-base --is-ancestor "$remote_tip" "$main_tip" 2>/dev/null; then
    remote_merged+=("$branch")
  fi
done < <(git branch -r --format='%(refname:short)')

# ── Print helpers ─────────────────────────────────────────────────────────────

print_table() {
  local color="$1"; shift
  local header1="$1"; shift
  local header2="$1"; shift
  local header3="$1"; shift
  printf "${color}${BLD}  %-42s %-28s %s${RST}\n" "$header1" "$header2" "$header3"
  printf "${color}  %-42s %-28s %s${RST}\n" "$(printf '%.0s─' {1..42})" "$(printf '%.0s─' {1..28})" "$(printf '%.0s─' {1..20})"
  for entry in "$@"; do
    IFS='|' read -r b c1 c2 <<< "$entry"
    printf "${color}  %-42s %-28s %s${RST}\n" "$b" "$c1" "$c2"
  done
}

echo
echo -e "${BLD}Branch Audit — $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)${RST}"
echo -e "Main branch: ${BLD}${MAIN}${RST}   Remote: ${BLD}${REMOTE}${RST}"
echo

# ── Merged local branches ─────────────────────────────────────────────────────
if [[ ${#merged_into_main[@]} -gt 0 ]]; then
  branches_to_delete=()
  worktrees_to_remove=()

  echo -e "${GRN}${BLD}✓ Merged into main (safe to delete locally)${RST}"
  printf "${GRN}${BLD}  %-42s %s${RST}\n" "Branch" "Worktree"
  printf "${GRN}  %-42s %s${RST}\n" "$(printf '%.0s─' {1..42})" "$(printf '%.0s─' {1..40})"

  for entry in "${merged_into_main[@]}"; do
    IFS='|' read -r b wt <<< "$entry"
    branches_to_delete+=("$b")
    if [[ -n "$wt" ]]; then
      worktrees_to_remove+=("$wt")
      printf "${GRN}  %-42s ${DIM}%s${RST}\n" "$b" "$wt"
    else
      printf "${GRN}  %s${RST}\n" "$b"
    fi
  done
  echo

  if $clean_local; then
    $dry_run && echo -e "  ${BLD}--clean-local --dry-run: showing commands that would run${RST}" \
             || echo -e "  ${BLD}--clean-local: removing worktrees and deleting branches...${RST}"
    for wt in "${worktrees_to_remove[@]}"; do
      run_cmd git worktree remove --force "$wt"
    done
    run_cmd git branch -d "${branches_to_delete[@]}"
    $dry_run || echo -e "  ${GRN}Done.${RST}"
  else
    if [[ ${#worktrees_to_remove[@]} -gt 0 ]]; then
      echo -e "  Remove worktrees first:"
      for wt in "${worktrees_to_remove[@]}"; do
        echo -e "    ${BLD}git worktree remove --force \"$wt\"${RST}"
      done
      echo
    fi
    echo -e "  Then delete branches: ${BLD}git branch -d ${branches_to_delete[*]}${RST}"
    echo -e "  Or run: ${BLD}$0 --clean-local${RST}"
  fi
  echo
fi

# ── Remote branches merged into main ─────────────────────────────────────────
if [[ ${#remote_merged[@]} -gt 0 ]]; then
  echo -e "${MAG}${BLD}☁  Remote branches merged into main (safe to delete on remote)${RST}"
  printf "${MAG}  %-42s${RST}\n" "$(printf '%.0s─' {1..42})"
  for b in "${remote_merged[@]}"; do
    printf "${MAG}  %s${RST}\n" "$b"
  done
  echo

  if $clean_remote; then
    $dry_run && echo -e "  ${BLD}--clean-remote --dry-run: showing commands that would run${RST}" \
             || echo -e "  ${BLD}--clean-remote: deleting merged remote branches...${RST}"
    run_cmd git push "$REMOTE" --delete "${remote_merged[@]}"
    $dry_run || echo -e "  ${GRN}Done.${RST}"
  else
    echo -e "  Delete with: ${BLD}git push $REMOTE --delete ${remote_merged[*]}${RST}"
    echo -e "  Or run: ${BLD}$0 --clean-remote${RST}"
  fi
  echo
fi

# ── Dirty (not merged, up-to-date with remote) ────────────────────────────────
if [[ ${#dirty[@]} -gt 0 ]]; then
  echo -e "${RED}${BLD}✗ Unmerged / dirty (not integrated into main)${RST}"
  print_table "$RED" "Branch" "Remote status" "Ahead of main" "${dirty[@]}"
  echo
fi

# ── Unpushed commits ──────────────────────────────────────────────────────────
if [[ ${#unpushed[@]} -gt 0 ]]; then
  echo -e "${YEL}${BLD}↑ Unpushed commits (local-only work)${RST}"
  print_table "$YEL" "Branch" "Push status" "Ahead of main" "${unpushed[@]}"
  echo
fi

# ── Diverged ─────────────────────────────────────────────────────────────────
if [[ ${#diverged[@]} -gt 0 ]]; then
  echo -e "${CYA}${BLD}⇅ Diverged from remote${RST}"
  print_table "$CYA" "Branch" "Divergence" "Ahead of main" "${diverged[@]}"
  echo
fi

if [[ ${#merged_into_main[@]} -eq 0 && ${#remote_merged[@]} -eq 0 && \
      ${#dirty[@]} -eq 0 && ${#unpushed[@]} -eq 0 && ${#diverged[@]} -eq 0 ]]; then
  echo -e "${GRN}All local branches (except current and main) are clean.${RST}"
fi
