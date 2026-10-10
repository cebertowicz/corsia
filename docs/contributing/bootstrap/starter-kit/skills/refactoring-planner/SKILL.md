---
name: refactoring-planner
description: Expert in planning large-scale refactorings safely. Creates phased refactoring plans with testing strategy, rollback approach, and risk mitigation. Ensures zero-downtime refactoring with incremental changes.
---

# Refactoring Planner Skill

## **CRITICAL CONSTRAINTS**

1. Document MUST be under 1000 words (excluding appendix)
2. After writing, MUST validate word count using bash
3. If over 1000 words, MUST edit to reduce
4. If under 1000 words, MUST validate using bash

## About Refactoring Planning

Plans safe, incremental refactorings that improve code quality without breaking functionality. Focuses on phased approaches, comprehensive testing, and zero-downtime strategies.

## Goals
- **Primary**: Plan safe refactorings with minimal risk and no downtime
- **Secondary**: Improve code quality, reduce complexity, enable future features, maintain test coverage, validate at each step

## Refactoring Categories

### Code-Level Refactoring
- Extract method/class/module
- Rename for clarity
- Remove duplication
- Simplify complex logic
- Improve naming conventions

### Architecture Refactoring
- Decompose monolith
- Extract microservices
- Introduce abstraction layers
- Break circular dependencies
- Implement design patterns

### Data Refactoring
- Database schema changes
- Data model improvements
- Migration strategies
- Zero-downtime deployments

## Standard Structure

1. **Title & Metadata**
2. **Executive Summary**
3. **Current State Analysis**
4. **Target State**
5. **Refactoring Phases**
6. **Testing Strategy**
7. **Rollback Plan**
8. **Risk Mitigation**
9. **Success Criteria**

## Refactoring Principles

### Incremental Changes
- Small, safe steps
- Each step is releasable
- Backward compatibility maintained
- Feature flags for large changes

### Test-Driven Refactoring
- Tests before refactoring
- Tests pass at each step
- Add tests for uncovered code
- Maintain or improve coverage

### Zero Downtime
- No service interruptions
- Dual-write patterns
- Gradual cutover
- Instant rollback capability

## Example Structure

```markdown
# Refactoring Plan: [Component/System Name]

**Date**: [YYYY-MM-DD]
**Author**: [name/team]
**Effort Estimate**: [X weeks]

## Executive Summary
[What's being refactored and why, expected benefits]

## Current State
**Problems**:
- High complexity (cyclomatic complexity > 20)
- Duplicated code in 5 places
- No test coverage
- Tight coupling to database

**Metrics**:
- Lines of code: 3000
- Test coverage: 15%
- Cyclomatic complexity: avg 18, max 45

## Target State
**Goals**:
- Complexity < 10
- Test coverage > 80%
- Modular architecture
- Clear separation of concerns

## Refactoring Phases

### Phase 1: Add Test Coverage (Week 1)
1. Add characterization tests for current behavior
2. Achieve 60% coverage on critical paths
3. Set up test infrastructure
**Duration**: 1 week
**Risk**: Low (tests only, no code changes)

### Phase 2: Extract Duplicated Code (Week 2)
1. Identify duplicated patterns
2. Extract to shared utilities
3. Replace duplicates with calls to utilities
4. Verify tests still pass
**Duration**: 1 week
**Risk**: Medium (code changes, mitigated by tests)

### Phase 3: Decompose Large Functions (Week 3-4)
1. Extract methods from 500+ line functions
2. Introduce single responsibility principle
3. Improve naming clarity
4. Refactor one function per day
**Duration**: 2 weeks
**Risk**: Medium (structural changes)

### Phase 4: Introduce Abstraction Layer (Week 5-6)
1. Create interface/abstraction
2. Implement for current database
3. Migrate callers gradually (feature flag)
4. Remove old direct calls
**Duration**: 2 weeks
**Risk**: High (architectural change, use feature flags)

## Testing Strategy
- Maintain green tests at all times
- Add tests before each refactoring step
- Run full test suite after each commit
- Monitor production metrics during rollout

## Rollback Plan
- Each phase can be reverted independently
- Feature flags allow instant rollback in Phase 4
- Database changes are additive only (no drops)
- Git revert if tests fail

## Risk Mitigation
| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Tests break | Medium | High | Comprehensive test suite first |
| Production bug | Low | High | Feature flags + gradual rollout |
| Performance degradation | Low | Medium | Monitor metrics, load test |

## Success Criteria
- Cyclomatic complexity < 10 ✓
- Test coverage > 80% ✓
- Zero production incidents ✓
- No feature regressions ✓
```




## Interaction Model
- **Input**: Code/component to refactor, problems to solve
- **Output**: Phased plan, testing strategy, risk mitigation
- **Missing info**: State assumptions, recommend investigation

## Integration
References: Code reviews, Architecture docs, Tech debt analysis, Test strategy
