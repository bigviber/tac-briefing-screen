import type { AdversaryTurn } from "../../api/types";

const SCRIPT: Omit<AdversaryTurn, "turn_index">[] = [
  { persona: "Marcus Hale · VP Sales", message: "Enterprise procurement always freezes in Q4. Compare 2023Q4 with 2024Q4 and tell me this is not just seasonality.", challenge_type: "alternative_explanation", cites: ["dso_2024q4"], concedes: false },
  { persona: "Marcus Hale · VP Sales", message: "Fine, 2023Q4 was 53 days. But the migration happened in between. How do you know the controller's 60–70% estimate is wrong?", challenge_type: "data_quality", cites: ["dso_2024q4", "ar_over_90_pct"], concedes: false },
  { persona: "Marcus Hale · VP Sales", message: "If we enforce 30-day terms on renewals we lose Vantor. Put a number on the churn risk versus the cash you recover.", challenge_type: "trade_off", cites: ["cash_trapped_usd"], concedes: false },
  { persona: "Marcus Hale · VP Sales", message: "Okay. The ramp concessions were approved by me, and the dunning holds were mine too. I can live with renewals on contracted terms if Finance owns the customer conversation.", challenge_type: "concession", cites: ["ramp_concessions_usd"], concedes: true },
];

export function adversaryTurn(index: number): AdversaryTurn {
  const s = SCRIPT[Math.min(Math.max(0, index - 1), SCRIPT.length - 1)];
  return { ...s, turn_index: index };
}
