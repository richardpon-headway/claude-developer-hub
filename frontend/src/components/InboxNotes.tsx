import { useQueryClient } from "@tanstack/react-query";

import { updateInboxNotes } from "../api/inbox";
import { NotesEditor } from "./NotesEditor";

interface Props {
  prRepo: string;
  prNumber: number;
  notes: string | null;
}

export function InboxNotes({ prRepo, prNumber, notes }: Props) {
  const queryClient = useQueryClient();
  return (
    <NotesEditor
      notes={notes}
      saveFn={(text) => updateInboxNotes(prRepo, prNumber, text)}
      onSaved={() => {
        queryClient.invalidateQueries({ queryKey: ["inbox"] });
      }}
    />
  );
}
