---
name: performance-analyzer
description: Expert in performance analysis and optimization. Analyzes bottlenecks, latency issues, and resource utilization. Creates optimization plans with prioritized recommendations and performance impact estimates.
---

# Performance Analyzer Skill

## Output discipline

Produce an evidence-backed result scaled to the user's request. Do not pad to an arbitrary word count; state material uncertainty and authority boundaries.


## About Performance Analysis

Performance analysis identifies bottlenecks, inefficiencies, and optimization opportunities in software systems. It uses metrics, profiling data, and monitoring to diagnose latency issues, resource constraints, and scalability problems. The goal is to provide actionable recommendations that improve response times, throughput, and resource efficiency while balancing development effort with performance gains.

## Goals
- **Primary**: Identify performance bottlenecks and provide prioritized optimization recommendations
- **Secondary**:
  - Diagnose latency and throughput issues
  - Analyze resource utilization (CPU, memory, disk, network)
  - Identify N+1 queries and inefficient database access
  - Evaluate caching opportunities
  - Assess scalability constraints
  - Estimate performance impact of optimizations
  - Balance quick wins vs long-term improvements

## Performance Dimensions

### Latency
- Response time (P50, P95, P99)
- Time to first byte (TTFB)
- Database query duration
- External API call duration
- Rendering/processing time

### Throughput
- Requests per second (RPS)
- Transactions per second (TPS)
- Data processing rate
- Message queue throughput

### Resource Utilization
- CPU usage
- Memory usage and leaks
- Disk I/O and IOPS
- Network bandwidth
- Connection pool saturation

### Efficiency
- Algorithmic complexity (O(n), O(n²), etc.)
- Database query efficiency (indexes, query plans)
- Caching effectiveness (hit rate, eviction rate)
- N+1 query problems

## Standard Performance Analysis Structure

Every performance analysis you write SHOULD follow this structure unless explicitly instructed otherwise:

1. **Title & Metadata**
   Clear system/component name with analysis date and baseline metrics.
2. **Executive Summary**
   High-level findings, critical bottlenecks, and top recommendations.
3. **Current Performance Baseline**
   Existing metrics and performance characteristics.
4. **Methodology**
   How the analysis was conducted (profiling, monitoring, load testing).
5. **Bottleneck Analysis**
   Identified performance issues with evidence and impact.
6. **Root Cause Analysis**
   Why bottlenecks exist (code, architecture, config, infrastructure).
7. **Optimization Recommendations**
   Prioritized improvements with effort estimates and expected gains.
8. **Quick Wins**
   Low-effort, high-impact optimizations to implement immediately.
9. **Long-Term Improvements**
   Strategic changes requiring more effort but significant gains.
10. **Performance Testing Plan**
    How to validate optimizations and prevent regressions.
11. **Appendix (optional)**
    Detailed metrics, query plans, profiling flame graphs, code snippets.

## Authoring Process

When asked to analyze performance:

1. **Establish baseline**
   Gather current performance metrics (latency, throughput, resource usage).
2. **Identify bottlenecks**
   Use profiling, monitoring, and load testing to find slow components.
3. **Analyze root causes**
   Determine why bottlenecks exist (inefficient code, missing indexes, N+1 queries, etc.).
4. **Prioritize issues**
   Rank by impact and effort using a priority matrix.
5. **Design optimizations**
   Propose specific improvements with expected performance gains.
6. **Estimate impact**
   Predict latency reduction, throughput increase, or resource savings.
7. **Enforce word limit**
   Keep the main analysis under ~1000 words to leave room for edits before final 1200-word check.

## Style Guidelines

- **Tone**: Data-driven, analytical, solution-focused. Emphasize measurable improvements.
- **Perspective**: Write in present tense for findings, imperative for recommendations.
- **Audience**: Engineers, architects, and technical leads.
- **Specificity**: Include exact metrics, percentiles, and measurements.
- **Evidence-based**: Support findings with profiling data, metrics, and query plans.
- **Impact-focused**: Prioritize by performance improvement, not just fixing everything.
- **Balanced**: Consider effort vs gain trade-offs.

When information is missing, state what additional data is needed (e.g. "Need query execution plan to confirm index usage").




## Performance Bottleneck Categories

### Database Bottlenecks
- Missing indexes
- N+1 query problems
- Full table scans
- Inefficient JOINs
- Lock contention
- Connection pool exhaustion

### Application Bottlenecks
- Inefficient algorithms (O(n²) or worse)
- Synchronous blocking operations
- Memory leaks
- Excessive object allocation
- CPU-intensive operations on request path

### Network Bottlenecks
- Excessive API calls
- Large payload sizes
- No compression
- Missing CDN for static assets
- High-latency external dependencies

### Infrastructure Bottlenecks
- Under-provisioned resources (CPU, memory)
- Disk I/O constraints
- Network bandwidth limits
- Auto-scaling not configured

### Caching Bottlenecks
- No caching where beneficial
- Cache stampede on expiration
- Low cache hit rate
- Inefficient cache key design

## Optimization Priority Matrix

| Impact | Effort Low | Effort Medium | Effort High |
|--------|-----------|---------------|-------------|
| **High** | **P0** (Do Now) | **P1** (Do Soon) | **P2** (Plan) |
| **Medium** | **P1** (Do Soon) | **P2** (Plan) | **P3** (Consider) |
| **Low** | **P2** (Plan) | **P3** (Consider) | **P4** (Defer) |

**P0**: Quick wins - high impact, low effort (hours to days)
**P1**: Important - high impact or low effort (days to 1 week)
**P2**: Strategic - plan and schedule (1-4 weeks)
**P3**: Optional - consider if capacity allows
**P4**: Defer - not worth effort at this time

## Example Performance Analysis Skeleton

Below is a minimal template this skill should follow by default. Adapt as needed.

```markdown
# Performance Analysis: [System/Component Name]

**System**: [name]
**Date**: [YYYY-MM-DD]
**Analyst**: [name/team]
**Load Scenario**: [expected traffic: 100 RPS, 1000 concurrent users, etc.]

## Executive Summary

[1-2 paragraph overview of performance findings, critical bottlenecks, and top 3 recommendations]

**Key Findings**:
- Finding 1: [e.g., "P95 latency 2.5s, target < 200ms"]
- Finding 2: [e.g., "N+1 queries causing 80% of database load"]
- Finding 3: [e.g., "Memory leak in service causing restarts every 4 hours"]

**Top Recommendations**:
1. [Quick win with high impact]
2. [Strategic improvement]
3. [Infrastructure optimization]

## Current Performance Baseline

### Latency Metrics
| Metric | Current | Target | Status |
|--------|---------|--------|--------|
| P50 latency | 450ms | < 100ms | ❌ 4.5x over |
| P95 latency | 2.5s | < 200ms | ❌ 12x over |
| P99 latency | 5.2s | < 500ms | ❌ 10x over |

### Throughput Metrics
| Metric | Current | Target | Status |
|--------|---------|--------|--------|
| Requests/sec | 50 RPS | 500 RPS | ❌ 10x under |
| Error rate | 2.3% | < 0.1% | ❌ 23x over |

### Resource Utilization
| Resource | Current | Threshold | Status |
|----------|---------|-----------|--------|
| CPU usage | 75% avg | < 60% | ⚠️ High |
| Memory usage | 85% avg | < 70% | ⚠️ High |
| DB connections | 95/100 | < 80 | ⚠️ Near max |

### Traffic Profile
- Peak traffic: 100 concurrent users
- Daily requests: ~50,000
- Geographic distribution: 60% US, 30% EU, 10% APAC

## Methodology

### Data Sources
- Application Performance Monitoring (APM): Datadog/New Relic traces
- Database monitoring: Slow query log, query execution plans
- Load testing: k6 with realistic traffic patterns
- Profiling: CPU and memory profiling over 24 hours

### Analysis Period
- [Date range, e.g., "7 days: Nov 25 - Dec 1, 2024"]
- Includes peak and off-peak traffic

## Bottleneck Analysis

### Bottleneck 1: N+1 Query Problem in User Listing
**Severity**: Critical
**Impact**: 80% of total database load, P95 latency 2.5s

**Evidence**:
- APM traces show 500+ individual SELECT queries per request
- Each user record triggers separate query for related data
- Database CPU spikes to 95% during peak hours

**Example**:
```typescript
// Current (inefficient)
const users = await db.user.findMany({ where: { workspaceId } });
for (const user of users) {
  user.profile = await db.profile.findUnique({ where: { userId: user.id } });
  user.permissions = await db.permission.findMany({ where: { userId: user.id } });
}
// Result: 1 + (2 * N) queries for N users
```

**Root Cause**: Missing use of eager loading / JOIN queries

**Estimated Fix Time**: 2-4 hours
**Expected Improvement**: P95 latency from 2.5s → 300ms (8x faster)

### Bottleneck 2: Missing Database Index
**Severity**: High
**Impact**: Full table scan on 5M row table, 15% of query time

**Evidence**:
- EXPLAIN ANALYZE shows Seq Scan on `documents` table
- Query takes 800ms avg, touches 5M rows to return 20 results
- Missing index on `workspaceId, createdAt`

**Query**:
```sql
SELECT * FROM documents
WHERE workspace_id = $1
ORDER BY created_at DESC
LIMIT 20;
-- Seq Scan: 800ms, 5M rows scanned
```

**Root Cause**: Index not created during initial migration

**Estimated Fix Time**: 30 minutes (create index)
**Expected Improvement**: Query time from 800ms → 5ms (160x faster)

### Bottleneck 3: Synchronous External API Calls
**Severity**: High
**Impact**: Blocks request processing, contributes 40% to P95 latency

**Evidence**:
- APM shows 600ms wait time for external API call on request path
- No parallelization, no caching, no timeout handling
- External API P95 = 800ms but request blocks until response

**Root Cause**: Synchronous design with no fallback or async processing

**Estimated Fix Time**: 1-2 days (refactor to async + caching)
**Expected Improvement**: P95 latency reduction of 600ms

## Root Cause Analysis

### Architecture Issues
- **N+1 queries**: No DataLoader pattern for batching
- **Synchronous design**: Blocking external calls on critical path
- **No caching**: Repeated expensive computations

### Database Issues
- **Missing indexes**: `workspace_id, created_at` on documents table
- **Connection pool**: Too small (100 connections, should be 200)
- **Query inefficiency**: No query optimization review

### Infrastructure Issues
- **Under-provisioned**: 2 CPU cores for 100 RPS is insufficient
- **No horizontal scaling**: Single instance, should be 3+ for redundancy
- **No CDN**: Static assets served from origin

## Optimization Recommendations

### P0: Quick Wins (Do Immediately)
1. **Add missing database index** (30 min)
   - Create index on `documents(workspace_id, created_at DESC)`
   - Expected: 160x faster queries, P95 -800ms
   - Risk: Low (read-only operation)

2. **Increase database connection pool** (15 min)
   - Change `max_connections` from 100 → 200
   - Expected: Eliminate connection wait times
   - Risk: Minimal (monitor DB resource usage)

3. **Add response caching for GET endpoints** (2-3 hours)
   - Implement Redis caching for repeated queries
   - Expected: 50% cache hit rate, P50 -200ms
   - Risk: Low (cache invalidation must be correct)

**Total P0 Impact**: P95 latency 2.5s → 700ms (3.5x faster)

### P1: Important Improvements (This Week)
1. **Fix N+1 queries with eager loading** (1 day)
   - Use Prisma `include` or raw JOINs
   - Expected: P95 -1.5s, 80% DB load reduction
   - Risk: Medium (test thoroughly for data consistency)

2. **Move external API calls to async background job** (2 days)
   - Use message queue for non-critical external calls
   - Expected: P95 -600ms
   - Risk: Medium (requires queue infrastructure)

3. **Horizontal scaling with load balancer** (2 days)
   - Deploy 3 instances behind ALB
   - Expected: 3x throughput, better failover
   - Risk: Low (standard practice)

**Total P1 Impact**: P95 latency 700ms → 150ms (17x total improvement from baseline)

### P2: Strategic Improvements (This Month)
1. **Implement DataLoader pattern** (1 week)
   - Batch and cache database queries
   - Expected: Further reduce DB queries by 60%

2. **Add CDN for static assets** (3 days)
   - CloudFront/CloudFlare in front of S3
   - Expected: TTFB reduction for assets from 500ms → 20ms

3. **Database read replicas** (1 week)
   - Separate read traffic to replicas
   - Expected: Primary DB load reduction by 70%

## Quick Wins (Implement This Week)

| Optimization | Effort | Impact | Priority |
|--------------|--------|--------|----------|
| Add database index | 30 min | 160x query speedup | P0 |
| Increase connection pool | 15 min | Eliminate waits | P0 |
| Add Redis caching | 3 hours | 50% cache hits | P0 |

**Combined Impact**: P95 latency from 2.5s → 700ms within 1 day

## Long-Term Improvements

1. **Microservices decomposition** (3 months)
   - Split monolith into domain services
   - Improves scalability and isolation
   - Major architectural change

2. **GraphQL Federation implementation** (2 months)
   - Distribute load across services
   - Better separation of concerns

3. **Auto-scaling based on metrics** (1 month)
   - Scale pods based on CPU/latency
   - Handle traffic spikes automatically

## Performance Testing Plan

### Validation Process
1. Implement optimization
2. Deploy to staging
3. Run load test with k6 (100-500 RPS)
4. Compare metrics vs baseline
5. Verify improvement matches estimate

### Success Criteria
- P95 latency < 200ms (from 2.5s)
- Throughput > 500 RPS (from 50 RPS)
- Error rate < 0.1% (from 2.3%)
- CPU usage < 60% at peak (from 75%)

### Regression Prevention
- Add performance tests to CI/CD
- Alert on P95 latency > 300ms
- Dashboard for daily performance review

## Appendix

### Detailed Query Plans
[EXPLAIN ANALYZE output for slow queries]

### Profiling Data
[CPU/memory flame graphs]

### Load Test Results
[k6 results before/after optimizations]

### Code Examples
[Specific code changes with before/after]
```

## Interaction Model for This Skill

When acting as the Performance Analyzer:

- **Input you expect**
  - System/component description
  - Performance metrics (latency, throughput, resource usage)
  - Profiling data, traces, or slow query logs
  - Performance targets/SLOs

- **Output you produce**
  - A single, comprehensive performance analysis following the template above
  - Prioritized optimization recommendations
  - Effort and impact estimates
  - Quick wins and strategic improvements
  - At the end, a short note confirming bash-based word-count validation and the final word count

- **If information is missing**
  - State what metrics or data are needed
  - Make reasonable assumptions based on typical systems
  - Provide general optimization patterns

## Integration with Related Documents

Performance analyses should align with and reference:
- **Architecture Documents**: For understanding system design
- **Monitoring Documentation**: For baseline metrics and dashboards
- **ADRs**: For architectural decisions affecting performance
- **Design Documents**: For optimization impact on features
- **Load Testing Results**: For validation data

Always check related documents for consistency and cross-reference appropriately.
