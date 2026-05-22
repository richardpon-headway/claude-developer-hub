import { useQueryClient } from "@tanstack/react-query";

import { updateAuthoredPrNotes } from "../api/authored";
import { NotesEditor } from "./NotesEditor";

interface Props {
  prRepo: string;
  prNumber: number;
  notes: string | null;
}

export function AuthoredPrNotes({ prRepo, prNumber, notes }: Props) {
  const queryClient = useQueryClient();
  return (
    <NotesEditor
      notes={notes}
      saveFn={(text) => updateAuthoredPrNotes(prRepo, prNumber, text)}
      onSaved={() => {
        queryClient.invalidateQueries({ queryKey: ["authored-prs"] });
      }}
    />
  );
}
