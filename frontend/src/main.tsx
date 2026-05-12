import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider, createRouter } from "@tanstack/react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as RadixTooltip from "@radix-ui/react-tooltip";

import "./styles.css";
import { routeTree } from "./routeTree.gen";

const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

const queryClient = new QueryClient();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RadixTooltip.Provider delayDuration={150} skipDelayDuration={100}>
        <RouterProvider router={router} />
      </RadixTooltip.Provider>
    </QueryClientProvider>
  </StrictMode>,
);
