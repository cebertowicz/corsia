---
name: test-strategy
description: Expert in test strategy and planning. Creates comprehensive test strategies covering unit, integration, e2e, performance, and security testing. Defines test pyramid, coverage goals, automation approach, and quality gates.
---

# Test Strategy Skill

## Output discipline

Produce an evidence-backed result scaled to the user's request. Do not pad to an arbitrary word count; state material uncertainty and authority boundaries.


## About Test Strategies

Test strategies define the comprehensive approach to testing a system, feature, or application. They establish the test pyramid, coverage goals, automation philosophy, testing tools, quality gates, and responsibilities. A well-defined test strategy ensures quality, prevents regressions, and enables confident deployment.

## Goals
- **Primary**: Define comprehensive testing approach that ensures quality and prevents regressions
- **Secondary**:
  - Establish appropriate test coverage at each layer (unit, integration, e2e)
  - Define automation strategy and CI/CD integration
  - Set quality gates and acceptance criteria
  - Identify testing tools and frameworks
  - Assign testing responsibilities
  - Enable fast feedback loops for developers
  - Balance test coverage with execution speed

## Test Pyramid Philosophy

The test pyramid guides test distribution:

```
      /\
     /E2E\      ← Few (slow, expensive, brittle)
    /------\
   /Integration\  ← Some (moderate speed/cost)
  /------------\
 /   Unit Tests  \ ← Many (fast, cheap, reliable)
/----------------\
```

### Unit Tests (Base - 70%)
- Fast (<1ms per test)
- Isolated (no external dependencies)
- Test single functions/methods
- Mock external dependencies
- High volume, high coverage

### Integration Tests (Middle - 20%)
- Moderate speed (ms to seconds)
- Test component interactions
- Use real dependencies (databases, APIs)
- Verify contracts between layers
- Moderate volume, focused coverage

### E2E Tests (Top - 10%)
- Slow (seconds to minutes)
- Test complete user workflows
- Real browsers, real systems
- Critical paths only
- Low volume, high-value scenarios

## Standard Test Strategy Structure

Every test strategy you write SHOULD follow this structure unless explicitly instructed otherwise:

1. **Title & Metadata**
   Clear scope with system/feature and testing levels covered.
2. **Executive Summary**
   High-level testing approach and quality goals.
3. **Scope**
   What will be tested and what's out of scope.
4. **Test Pyramid Distribution**
   Target percentages and rationale for each test layer.
5. **Testing Approach by Layer**
   Detailed strategy for unit, integration, e2e, performance, security.
6. **Test Coverage Goals**
   Coverage targets and critical path identification.
7. **Test Automation**
   CI/CD integration, test execution frequency, and tooling.
8. **Quality Gates**
   Pass/fail criteria for builds and deployments.
9. **Testing Tools & Frameworks**
   Stack, libraries, and infrastructure.
10. **Responsibilities**
    Who writes, maintains, and reviews tests.
11. **Appendix (optional)**
    Test examples, configuration files, test data strategies.

If the organization provides an existing test strategy template, adapt to that while preserving the above logical sections.

## Authoring Process

When asked to write a test strategy:

1. **Understand the system**
   Review architecture, tech stack, and critical user workflows.
2. **Identify risk areas**
   Determine high-value test scenarios and failure modes.
3. **Define test pyramid**
   Establish appropriate distribution across test layers.
4. **Select tools**
   Choose testing frameworks compatible with tech stack.
5. **Set coverage goals**
   Define realistic, meaningful coverage targets.
6. **Plan automation**
   Integrate tests into CI/CD pipeline.
7. **Enforce word limit**
   Keep the main strategy under ~1000 words to leave room for edits before final 1200-word check.

## Style Guidelines

- **Tone**: Practical, quality-focused, balanced. Emphasize pragmatic testing.
- **Perspective**: Write in present/future tense for strategy, imperative for requirements.
- **Audience**: Engineers, QA team, technical leads, and product managers.
- **Specificity**: Reference specific tools, frameworks, and coverage metrics.
- **Prioritization**: Focus on high-value tests, avoid test-for-test's-sake mentality.
- **Pragmatic**: Balance perfect coverage with practical constraints (time, resources).
- **Evidence-based**: Reference industry standards and best practices.

When information is missing, explicitly state assumptions (e.g. "Assuming Jest for unit testing based on React/Node.js stack").




## Testing Types Covered

### Unit Testing
- Function-level tests
- Class/module tests
- Pure logic testing
- Mocking external dependencies
- Fast execution (<1ms per test)

### Integration Testing
- API endpoint tests
- Database integration tests
- Service-to-service communication
- Message queue integration
- External service mocking (when appropriate)

### End-to-End (E2E) Testing
- Full user workflow simulation
- Browser automation
- Critical path coverage
- Cross-browser testing (if applicable)
- Mobile app flows

### Performance Testing
- Load testing (expected traffic)
- Stress testing (peak + beyond)
- Endurance testing (sustained load)
- Spike testing (sudden traffic bursts)
- Latency and throughput measurement

### Security Testing
- Authentication/authorization tests
- Input validation and sanitization
- SQL injection prevention
- XSS prevention
- OWASP Top 10 coverage

### Contract Testing
- API contract validation
- GraphQL schema compliance
- Event schema validation
- Backward compatibility

## Test Coverage Philosophy

### Coverage Metrics
- **Line coverage**: Percentage of code lines executed (target: 80%+)
- **Branch coverage**: Percentage of decision branches tested (target: 70%+)
- **Function coverage**: Percentage of functions called (target: 90%+)
- **Critical path coverage**: 100% for business-critical workflows

### Coverage ≠ Quality
- High coverage doesn't guarantee quality
- Focus on meaningful assertions, not just execution
- Prioritize testing critical paths and edge cases
- Avoid "testing for coverage" mentality

## Quality Gates

Define pass/fail criteria:

### Pre-Merge Gates (Required)
- All tests pass (0 failures)
- Unit test coverage ≥ target (e.g., 80%)
- No high/critical security vulnerabilities
- Linting passes
- Type checking passes (TypeScript)

### Pre-Deploy Gates (Staging)
- All tests pass (unit, integration, e2e)
- Performance tests meet SLO targets
- Security scan passes
- No known critical bugs

### Pre-Deploy Gates (Production)
- Successful staging deployment for 24+ hours
- Zero critical incidents in staging
- All quality gates passed
- Rollback plan validated

## Example Test Strategy Skeleton

Below is a minimal template this skill should follow by default. Adapt as needed.

```markdown
# Test Strategy: [System/Feature Name]

**Author**: [Name/Team]
**Date**: [YYYY-MM-DD]
**Scope**: [System/feature being tested]
**Tech Stack**: [Languages, frameworks, platforms]

## Executive Summary

[1-2 paragraph overview of testing approach, quality goals, and key decisions]

## Scope

### In Scope
- [Components, features, or workflows to be tested]
- [Test types: unit, integration, e2e, performance, security]

### Out of Scope
- [What will NOT be tested]
- [Dependencies handled by other teams]
- [Third-party services (covered by vendor testing)]

## Test Pyramid Distribution

**Target Distribution**:
- Unit Tests: 70% (~[X] tests)
- Integration Tests: 20% (~[Y] tests)
- E2E Tests: 10% (~[Z] tests)

**Rationale**: [Why this distribution makes sense for this system]

## Testing Approach by Layer

### Unit Testing
**Scope**: [What gets unit tested]
**Tools**: [Framework, assertion library, mocking library]
**Coverage Goal**: [Percentage, e.g., 80% line coverage]
**Key Focus**:
- Business logic validation
- Edge case handling
- Error conditions
- Data transformations

**Example Scenarios**:
- [Scenario 1]
- [Scenario 2]

### Integration Testing
**Scope**: [What gets integration tested]
**Tools**: [Testing framework, database/API mocking tools]
**Coverage Goal**: [Key integration points]
**Key Focus**:
- API endpoint validation
- Database queries and transactions
- Service-to-service communication
- Authentication/authorization flows

**Example Scenarios**:
- [Scenario 1]
- [Scenario 2]

### E2E Testing
**Scope**: [Critical user workflows]
**Tools**: [Playwright, Cypress, Selenium, etc.]
**Coverage Goal**: [Number of critical paths]
**Key Focus**:
- Happy path workflows
- Critical business flows
- Cross-browser compatibility (if applicable)
- Regression prevention

**Example Scenarios**:
- [Critical workflow 1]
- [Critical workflow 2]

### Performance Testing
**Scope**: [Load testing scenarios]
**Tools**: [k6, JMeter, Artillery, etc.]
**Performance Targets**:
- P95 latency: [target, e.g., < 200ms]
- Throughput: [target, e.g., 1000 RPS]
- Error rate: [target, e.g., < 0.1%]

**Test Scenarios**:
- Baseline load: [expected daily traffic]
- Peak load: [2x baseline]
- Stress: [5x baseline until failure]

### Security Testing
**Scope**: [Security test areas]
**Tools**: [SAST, DAST, dependency scanning]
**Coverage**:
- OWASP Top 10 validation
- Authentication/authorization tests
- Input validation and sanitization
- Secrets management

**Test Scenarios**:
- [Security scenario 1]
- [Security scenario 2]

## Test Coverage Goals

### Overall Targets
- Line coverage: ≥ 80%
- Branch coverage: ≥ 70%
- Function coverage: ≥ 90%
- Critical path coverage: 100%

### Critical Paths (Must be 100% covered)
1. [Critical workflow 1, e.g., "User registration and login"]
2. [Critical workflow 2, e.g., "Payment processing"]
3. [Critical workflow 3, e.g., "Data export"]

### Acceptable Gaps
- [Areas with lower coverage and rationale, e.g., "Generated code"]
- [UI styling (visual regression instead of unit tests)]

## Test Automation

### CI/CD Integration
**On Pull Request**:
- Run unit tests (parallel execution, ~2 min)
- Run integration tests (~5 min)
- Run linting and type checking
- Block merge if any tests fail

**On Merge to Main**:
- Run full test suite (unit + integration + e2e, ~15 min)
- Run security scans
- Deploy to staging if all pass

**Nightly**:
- Run full E2E suite (~30 min)
- Run performance tests
- Run extended security scans

### Test Execution Environment
- Unit/Integration: Run in CI (GitHub Actions, Jenkins, etc.)
- E2E: Run in dedicated test environment with production-like data
- Performance: Run in isolated environment with load generation

### Test Data Strategy
- Unit tests: Use fixtures and mocks
- Integration tests: Use test database with seeded data
- E2E tests: Use isolated test tenant with realistic data
- Reset test data between runs

## Quality Gates

### Pre-Merge (Blocks PR)
- ✅ All unit tests pass (0 failures)
- ✅ Integration tests pass
- ✅ Coverage ≥ 80% (or no decrease from baseline)
- ✅ No high/critical security vulnerabilities
- ✅ Linting and type checking pass

### Pre-Deploy to Staging
- ✅ All tests pass (unit, integration, e2e)
- ✅ No critical/high bugs in Jira
- ✅ Security scan passes

### Pre-Deploy to Production
- ✅ Staging deployment successful for 24+ hours
- ✅ Zero critical incidents in staging
- ✅ Performance tests meet SLO targets
- ✅ Manual QA sign-off (if required)

## Testing Tools & Frameworks

### Unit Testing
- **Framework**: [Jest, Vitest, pytest, JUnit, etc.]
- **Assertion Library**: [expect, chai, assert, etc.]
- **Mocking**: [jest.mock, sinon, unittest.mock, etc.]

### Integration Testing
- **Framework**: [Same as unit or separate, e.g., Supertest, TestContainers]
- **Database**: [In-memory DB or Docker containers]

### E2E Testing
- **Framework**: [Playwright, Cypress, Selenium, Detox (mobile)]
- **Browser**: [Chromium, Firefox, Safari]

### Performance Testing
- **Tool**: [k6, JMeter, Artillery, Locust]

### Security Testing
- **SAST**: [ESLint security plugin, Semgrep, SonarQube]
- **DAST**: [OWASP ZAP, Burp Suite]
- **Dependency Scanning**: [Snyk, Dependabot, npm audit]

### CI/CD
- **Platform**: [GitHub Actions, GitLab CI, Jenkins, CircleCI]
- **Test Parallelization**: [Jest --maxWorkers, Playwright sharding]

## Responsibilities

### Development Team
- Write unit tests for all new code
- Write integration tests for new APIs/services
- Maintain existing tests (fix flaky tests)
- Achieve coverage targets before merge
- Review test quality in code reviews

### QA Team (if separate)
- Write and maintain E2E tests
- Perform exploratory testing
- Define test scenarios for critical paths
- Manual testing for edge cases
- Performance and security testing coordination

### DevOps/SRE
- Maintain CI/CD pipeline
- Manage test environments
- Monitor test execution performance
- Investigate test infrastructure issues

### Product Team
- Define acceptance criteria
- Provide test scenarios for critical workflows
- Sign off on quality gates (if required)

## Test Maintenance

### Flaky Test Policy
- Flaky tests (intermittent failures) must be fixed or disabled within 48 hours
- Track flaky test rate (target: < 1%)
- Root cause analysis for recurring flaky tests

### Test Performance
- Unit test suite should run in < 5 minutes
- Integration test suite should run in < 10 minutes
- E2E test suite should run in < 30 minutes
- Optimize slow tests or move to nightly runs

### Test Data Management
- Refresh test data weekly
- Anonymize production data for testing (no real PHI/PII)
- Version control test fixtures

## Appendix (Optional)

### Example Unit Test
[Code snippet showing typical unit test]

### Example Integration Test
[Code snippet showing typical integration test]

### Example E2E Test
[Code snippet showing typical E2E test]

### CI Configuration
[Sample GitHub Actions or Jenkins pipeline config]
```

## Example Test Strategy (Realistic, Short)

```markdown
# Test Strategy: Patient Portal API

**Author**: QA Team
**Date**: 2024-12-01
**Scope**: Patient Portal API (authentication, PHI access, appointments)
**Tech Stack**: Node.js, TypeScript, GraphQL, Postgres, React

## Executive Summary

This strategy defines the testing approach for the Patient Portal API, a HIPAA-compliant system handling patient health information and appointment scheduling. Testing prioritizes security, data integrity, and reliability. We use a 70/20/10 test pyramid with strong unit test coverage (80%+), focused integration testing on GraphQL resolvers and database operations, and E2E tests covering critical patient workflows. All tests run in CI/CD with quality gates blocking deployment on failure.

## Scope

### In Scope
- Patient Portal API (GraphQL resolvers, services, data layer)
- Authentication and authorization
- PHI access logging and compliance
- Appointment scheduling workflows
- Unit, integration, E2E, performance, and security testing

### Out of Scope
- Frontend React components (separate test strategy)
- Third-party integrations (EHR system, payment processor)
- Infrastructure (covered by SRE team)

## Test Pyramid Distribution

**Target Distribution**:
- Unit Tests: 70% (~500 tests)
- Integration Tests: 20% (~140 tests)
- E2E Tests: 10% (~70 tests)

**Rationale**: Heavy unit testing ensures business logic correctness and fast feedback. Integration tests validate GraphQL API contracts and database interactions. E2E tests cover critical patient workflows to prevent regressions.

## Testing Approach by Layer

### Unit Testing
**Scope**: Business logic, services, utilities, GraphQL resolvers (mocked dependencies)
**Tools**: Jest, ts-jest, @testing-library for React components
**Coverage Goal**: 80% line coverage, 70% branch coverage

**Key Focus**:
- Authentication logic (password hashing, JWT generation)
- Authorization checks (workspaceId filtering, role validation)
- Appointment scheduling business rules
- PHI access logging
- Input validation and sanitization

**Example Scenarios**:
- User login with valid/invalid credentials
- Appointment booking with date/time conflicts
- PHI access audit log creation

### Integration Testing
**Scope**: GraphQL API endpoints, database queries, service integration
**Tools**: Jest, Supertest, TestContainers (Postgres)
**Coverage Goal**: All GraphQL resolvers, critical database queries

**Key Focus**:
- GraphQL query and mutation execution
- Database transactions (appointment creation)
- Multi-tenancy isolation (workspaceId filtering)
- Error handling and GraphQL error responses

**Example Scenarios**:
- Query appointments for patient with proper workspaceId filtering
- Create appointment with database transaction rollback on error
- Unauthorized access returns proper GraphQL error

### E2E Testing
**Scope**: Critical patient workflows (browser automation)
**Tools**: Playwright
**Coverage Goal**: 5 critical workflows

**Key Focus**:
- Patient login and authentication
- View medical records (PHI access)
- Schedule appointment
- Cancel appointment
- Password reset flow

**Example Scenarios**:
- Patient logs in → views appointments → schedules new appointment → confirms success
- Patient attempts unauthorized access → gets error → redirected to login

### Performance Testing
**Scope**: API load testing under expected and peak traffic
**Tools**: k6
**Performance Targets**:
- P95 latency: < 200ms for queries, < 500ms for mutations
- Throughput: 500 RPS sustained
- Error rate: < 0.1%

**Test Scenarios**:
- Baseline: 100 concurrent users (expected daily peak)
- Peak: 500 concurrent users (2x growth)
- Stress: 1000+ concurrent users until degradation

### Security Testing
**Scope**: OWASP Top 10, authentication, authorization, PHI protection
**Tools**: ESLint security plugin, npm audit, OWASP ZAP (nightly)
**Coverage**:
- SQL injection prevention (parameterized queries)
- Authentication bypass attempts
- Authorization checks (workspaceId isolation)
- PHI access without proper authorization

**Test Scenarios**:
- Attempt to access another tenant's PHI → reject with 403
- Malformed JWT token → reject with 401
- SQL injection in appointment search → safely handle

## Test Coverage Goals

### Overall Targets
- Line coverage: ≥ 80%
- Branch coverage: ≥ 70%
- Function coverage: ≥ 90%
- Critical path coverage: 100%

### Critical Paths (Must be 100% covered)
1. Patient authentication and session management
2. PHI access with audit logging
3. Appointment scheduling and cancellation
4. Multi-tenancy workspaceId filtering

### Acceptable Gaps
- Generated GraphQL types (auto-generated by codegen)
- Database migration scripts (manually tested in staging)

## Test Automation

### CI/CD Integration
**On Pull Request**:
- Run unit tests (~3 min, parallel)
- Run integration tests (~7 min, TestContainers)
- Run linting, type checking, security scan
- Block merge if any fail or coverage drops below 80%

**On Merge to Main**:
- Run full test suite (unit + integration + E2E, ~20 min)
- Deploy to staging if all pass
- Run smoke tests on staging

**Nightly**:
- Run full E2E suite (~45 min)
- Run performance tests with k6
- Run OWASP ZAP security scan

### Test Execution Environment
- Unit/Integration: GitHub Actions (Ubuntu runner)
- E2E: Staging environment (production-like, isolated test tenant)
- Performance: Dedicated load testing environment

### Test Data Strategy
- Unit: Mock data and fixtures
- Integration: TestContainers Postgres with seeded data (reset per test)
- E2E: Isolated test tenant with realistic anonymized PHI
- Performance: Load generation with synthetic patient data

## Quality Gates

### Pre-Merge (Blocks PR)
- ✅ All unit and integration tests pass (0 failures)
- ✅ Coverage ≥ 80% (or no decrease)
- ✅ No high/critical npm vulnerabilities
- ✅ Linting and TypeScript checks pass

### Pre-Deploy to Staging
- ✅ All tests pass (including E2E)
- ✅ No critical bugs in Jira
- ✅ Security scan passes

### Pre-Deploy to Production
- ✅ Staging stable for 24+ hours
- ✅ Zero critical incidents in staging
- ✅ Performance tests meet SLO targets (P95 < 200ms)
- ✅ Manual QA sign-off

## Testing Tools & Frameworks

### Unit Testing
- **Framework**: Jest 29 with ts-jest
- **Assertion**: expect (Jest built-in)
- **Mocking**: jest.mock, jest.fn()

### Integration Testing
- **Framework**: Jest + Supertest
- **Database**: TestContainers (Postgres 15)
- **GraphQL**: apollo-server-testing

### E2E Testing
- **Framework**: Playwright (Chromium)
- **Browser**: Headless Chrome in CI, headed locally

### Performance Testing
- **Tool**: k6 (Go-based load testing)

### Security Testing
- **SAST**: ESLint security plugin, SonarQube
- **DAST**: OWASP ZAP (nightly scans)
- **Dependencies**: npm audit, Snyk

### CI/CD
- **Platform**: GitHub Actions
- **Parallelization**: Jest --maxWorkers=4

## Responsibilities

### Development Team
- Write unit tests for all new code (target: 80% coverage)
- Write integration tests for new GraphQL resolvers
- Fix flaky tests within 48 hours
- Review test quality in PRs

### QA Team
- Write and maintain E2E tests for critical workflows
- Perform exploratory testing weekly
- Define test scenarios for new features
- Coordinate performance and security testing

### DevOps/SRE
- Maintain GitHub Actions pipeline
- Manage staging and test environments
- Monitor test execution time (alert if > 30 min)

### Product Team
- Define acceptance criteria for features
- Provide test scenarios for critical workflows
- Sign off on pre-production deployments

## Test Maintenance

### Flaky Test Policy
- Flaky tests must be fixed or quarantined within 48 hours
- Track flaky test rate: Target < 1%, alert if > 3%
- Monthly review of quarantined tests

### Test Performance
- Unit tests: < 5 min (currently: 3 min)
- Integration tests: < 10 min (currently: 7 min)
- E2E tests: < 30 min (currently: 20 min)
- Optimize tests exceeding thresholds or move to nightly

### Test Data Management
- Refresh staging test data weekly
- Anonymize production data (no real PHI)
- Version control test fixtures in `/tests/fixtures/`

## Appendix

### Example Unit Test
```typescript
describe('PatientService.scheduleAppointment', () => {
  it('should create appointment with valid date/time', async () => {
    const service = new PatientService(mockDb);
    const result = await service.scheduleAppointment({
      patientId: 'p1',
      providerId: 'doc1',
      dateTime: '2024-12-15T10:00:00Z',
      workspaceId: 'ws1'
    });
    expect(result.status).toBe('confirmed');
    expect(mockDb.appointments.create).toHaveBeenCalledWith(
      expect.objectContaining({ workspaceId: 'ws1' })
    );
  });
});
```

### CI Configuration
```yaml
# .github/workflows/test.yml
name: Test
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-node@v3
      - run: npm ci
      - run: npm run test:unit -- --coverage
      - run: npm run test:integration
      - run: npm run lint
      - run: npm run typecheck
```
```

## Interaction Model for This Skill

When acting as the Test Strategy Writer:

- **Input you expect**
  - System/feature description
  - Tech stack (languages, frameworks, platforms)
  - Critical workflows or risk areas
  - Quality requirements or SLOs

- **Output you produce**
  - A single, comprehensive test strategy following the template above
  - Clear test pyramid distribution with rationale
  - Specific testing approach for each layer
  - Coverage goals and quality gates
  - At the end, a short note confirming bash-based word-count validation and the final word count

- **If information is missing**
  - Make reasonable assumptions based on tech stack
  - State assumptions explicitly
  - Recommend tools based on industry standards

## Integration with Related Documents

Test strategies should align with and reference:
- **Architecture Documents**: For understanding system design and risk areas
- **ADRs**: For technology choices affecting test tools
- **Design Documents**: For critical workflows to test
- **Quality Standards**: For coverage and quality gate requirements
- **CI/CD Documentation**: For pipeline integration details

Always check related documents for consistency and cross-reference appropriately.
