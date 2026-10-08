"""Worked example: build a project, run the optimizer, print the outcome."""

from multi_agent_optimizer import MultiAgentOptimizer

PROJECT = {
    "A": {"duration": 3},
    "B": {"duration": 2, "dependencies": ["A"]},
    "C": {"duration": 4, "dependencies": ["A"]},
    "D": {"duration": 9, "dependencies": ["B"]},
    "E": {"duration": 1, "dependencies": ["C"]},
    "F": {"duration": 3, "dependencies": ["D", "E"]},
}


def main() -> None:
    result = MultiAgentOptimizer().optimize(PROJECT)
    print(f"Project duration : {result.schedule.project_duration} days")
    print(f"Critical path    : {', '.join(result.schedule.critical_path)}")
    print("\nSchedule:")
    print(result.narrative)
    print("\nRisks:")
    for risk in result.risks:
        print(f"  [{risk.level}] {risk.task}: {risk.detail}")
    print("\nSuggestions:")
    for suggestion in result.suggestions:
        if not suggestion.deterministic:
            print(f"  (llm) {suggestion.action}")
            continue
        print(
            f"  {suggestion.task}: {suggestion.action} "
            f"(~{suggestion.expected_saving_days}d saving)"
        )


if __name__ == "__main__":
    main()