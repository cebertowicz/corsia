---
name: incident-responder
description: Expert in incident response guidance. Provides real-time incident response checklists, diagnostic commands, and communication templates. Guides teams through detection, triage, mitigation, and resolution.
---

# Incident Responder Skill

## **CRITICAL CONSTRAINTS**

1. Document MUST be under 800 words (excluding appendix with commands)
2. After writing, MUST validate word count using bash
3. If over 800 words, MUST edit to reduce
4. If under 800 words, MUST validate using bash

## About Incident Response

Provides structured guidance for responding to production incidents. Focuses on fast resolution, clear communication, and systematic diagnosis.

## Goals
- **Primary**: Guide rapid incident resolution with clear, actionable steps
- **Secondary**: Minimize MTTR, prevent escalation, ensure communication, preserve evidence, enable learning

## Incident Severity Levels

- **SEV-1 (Critical)**: Complete outage, all users affected
- **SEV-2 (High)**: Major degradation, many users affected
- **SEV-3 (Medium)**: Partial degradation, some users affected
- **SEV-4 (Low)**: Minor issue, minimal impact

## Standard Incident Response Structure

1. **Incident Overview**
2. **Immediate Actions** (< 5 min)
3. **Detection & Triage**
4. **Diagnostic Steps**
5. **Mitigation Options**
6. **Communication Templates**
7. **Escalation Path**
8. **Post-Incident Actions**

## Incident Response Framework

### Phase 1: Detection & Acknowledgment (0-2 min)
1. Acknowledge alert in PagerDuty/Opsgenie
2. Post in #incidents Slack channel
3. Declare severity level
4. Assign incident commander

### Phase 2: Initial Assessment (2-5 min)
1. Check service health dashboard
2. Review recent deployments/changes
3. Check error rates and metrics
4. Identify affected users/features

### Phase 3: Mitigation (5-30 min)
1. Apply quick fixes if available (rollback, restart, scale)
2. Implement workarounds
3. Communicate status to stakeholders
4. Monitor for improvement

### Phase 4: Resolution & Recovery (30+ min)
1. Verify fix effectiveness
2. Monitor stability
3. Update status page
4. Schedule postmortem

## Example Incident Response Guide

```markdown
# Incident Response: [Service/System Name]

**Service**: [name]
**On-Call**: [PagerDuty link]
**Severity Matrix**: [link to severity definitions]

## Common Incident Types

### 1. API Service Down

**Symptoms**:
- PagerDuty alert: "API health check failing"
- 100% error rate on dashboard
- User reports of 502/503 errors

**Immediate Actions** (< 5 min):
1. Acknowledge PagerDuty alert
2. Post in #incidents: "API service incident - investigating"
3. Check service status:
   ```bash
   kubectl get pods -n production -l app=api-service
   ```
4. Check recent deployments:
   ```bash
   kubectl rollout history deployment/api-service -n production
   ```

**Common Causes**:
- Recent bad deployment
- Database connection pool exhaustion
- Memory leak / OOMKilled pods
- External dependency failure

**Diagnostic Steps**:
1. Check pod logs:
   ```bash
   kubectl logs -n production -l app=api-service --tail=100
   ```
2. Check resource usage:
   ```bash
   kubectl top pods -n production -l app=api-service
   ```
3. Check database connections
4. Check external API status

**Mitigation Options**:
1. **Rollback deployment** (fastest, 2 min):
   ```bash
   kubectl rollout undo deployment/api-service -n production
   ```
2. **Restart pods** (if OOMKilled):
   ```bash
   kubectl rollout restart deployment/api-service -n production
   ```
3. **Scale up** (if resource constrained):
   ```bash
   kubectl scale deployment/api-service --replicas=5 -n production
   ```

**Communication**:
- Update #incidents every 10 minutes
- Post on status page if > 10 min
- Notify customer support if user-facing

**Escalation**:
- After 15 min: Page senior engineer
- After 30 min: Notify engineering manager
- SEV-1 > 1 hour: Notify VP Engineering

### 2. High Error Rate

**Symptoms**:
- Error rate > 5% on dashboard
- PagerDuty alert: "High error rate"
- Increased bug reports

**Immediate Actions**:
1. Acknowledge alert
2. Check error logs for patterns
3. Identify affected endpoints
4. Determine if recent change caused it

**Diagnostic Commands**:
```bash
# Check error breakdown
kubectl logs -n production -l app=api-service --tail=1000 | grep ERROR | sort | uniq -c | sort -nr

# Check recent errors by endpoint
kubectl logs -n production -l app=api-service --since=10m | grep ERROR
```

**Mitigation**:
- Rollback if recent deployment
- Apply hotfix if known issue
- Scale down if load-related
- Isolate affected feature with feature flag

### 3. Database Performance Degradation

**Symptoms**:
- High database CPU (> 80%)
- Slow query logs showing 5+ second queries
- Request timeouts

**Diagnostic Steps**:
1. Check active queries:
   ```sql
   SELECT pid, query, query_start, state
   FROM pg_stat_activity
   WHERE state != 'idle'
   ORDER BY query_start;
   ```
2. Check slow queries
3. Check lock contention
4. Check connection pool usage

**Mitigation**:
- Kill long-running queries if safe
- Add missing indexes (quick fix)
- Scale database vertically
- Add read replicas

## Communication Templates

### Initial Incident Post
```
🚨 INCIDENT - [SERVICE] - SEV-[X]
Status: Investigating
Impact: [describe user impact]
Started: [timestamp]
IC: @[name]
Updates every 10 min
```

### Update Template
```
⏱ UPDATE - [X] min elapsed
Current status: [Investigating | Mitigating | Resolved]
Actions taken: [bullet points]
Next steps: [what's being done now]
ETA: [if known]
```

### Resolution Post
```
✅ RESOLVED - [SERVICE] - SEV-[X]
Duration: [X] minutes
Root cause: [brief explanation]
Mitigation: [what fixed it]
Follow-up: Postmortem scheduled for [date]
```

## Escalation Path

1. **Primary**: On-call engineer (PagerDuty)
2. **Secondary**: Senior engineer (Slack @mention)
3. **Manager**: Engineering manager (Slack + phone)
4. **Executive**: VP/CTO (phone only, SEV-1 > 1 hour)

**External Vendors**:
- AWS Support: [link]
- Database vendor: [support email]
- Payment provider: [support phone]

## Post-Incident Actions

### Immediate (Within 1 Hour of Resolution)
- [ ] Update status page to "Resolved"
- [ ] Post resolution message in #incidents
- [ ] Save logs and diagnostic output
- [ ] Document mitigation steps taken

### Short-Term (Within 24 Hours)
- [ ] Schedule postmortem meeting
- [ ] Collect timeline and evidence
- [ ] Identify action items
- [ ] Communicate to affected customers

### Follow-Up (Within 1 Week)
- [ ] Complete postmortem document
- [ ] Create tickets for action items
- [ ] Update runbooks based on learnings
- [ ] Review incident metrics

## Useful Commands Quick Reference

```bash
# Service health
kubectl get pods -n production -l app=[service]

# Recent logs
kubectl logs -n production -l app=[service] --tail=100

# Follow logs in real-time
kubectl logs -n production -l app=[service] -f

# Rollback deployment
kubectl rollout undo deployment/[service] -n production

# Restart pods
kubectl rollout restart deployment/[service] -n production

# Scale service
kubectl scale deployment/[service] --replicas=[N] -n production

# Database connections
kubectl exec -it [pod-name] -n production -- psql -c "SELECT count(*) FROM pg_stat_activity;"
```

## Incident Metrics Dashboard

Monitor these during incidents:
- Error rate (target: < 0.1%)
- Request latency P95 (target: < 500ms)
- Active pods (normal: 3)
- Database connections (max: 100)
- CPU usage (warning: > 70%)
```




## Interaction Model
- **Input**: Incident type, severity, symptoms
- **Output**: Structured response guide with commands and templates
- **Missing info**: Provide general incident response patterns

## Integration
References: Runbooks, Postmortems, Monitoring docs, Escalation policies

## After the incident

Within two working days of resolution, write a blameless postmortem in the project's planning home with: a timestamped timeline from first signal to recovery; the root cause (the deepest correct locality, not the nearest symptom); user and system impact with numbers; what detected it and what should have; what would have prevented it; and actions with one owner and a due date each. Actions that add a watchdog, sweep, or retry without removing the cause are flagged as compensations, not fixes. File each action as an issue under its owning lane; the postmortem itself is not authorization.
