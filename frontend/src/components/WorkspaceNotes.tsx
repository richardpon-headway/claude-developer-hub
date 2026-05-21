import { useQueryClient } from "@tanstack/react-query";

import { updateNotes } from "../api/worktrees";
import { NotesEditor } from "./NotesEditor";

interface Props {
  repo: string;
  name: string;
  notes: string | null;
}

export function WorkspaceNotes({ repo, name, notes }: Props) {
  const queryClient = useQueryClient();
  return (
    <NotesEditor
      notes={notes}
      saveFn={(text) => updateNotes(repo, name, text)}
      onSaved={() => {
        queryClient.invalidateQueries({ queryKey: ["worktrees"] });
        queryClient.invalidateQueries({ queryKey: ["worktree", repo, name] });
      }}
    />
  );
}
