---
name: security-review
description: Expert in security reviews for code, architecture, and designs. Identifies vulnerabilities against OWASP Top 10, HIPAA, SOC2, and platform security standards. Produces actionable findings with severity and remediation guidance.
---

# Security Review Skill

## Output discipline

Produce an evidence-backed result scaled to the user's request. Do not pad to an arbitrary word count; state material uncertainty and authority boundaries.


## About Security Reviews

Security reviews are systematic evaluations of code, architecture, or design documents to identify security vulnerabilities, weaknesses, and compliance gaps. They serve as a proactive measure to prevent security incidents, protect sensitive data, and ensure regulatory compliance.

## Goals
- **Primary**: Identify security vulnerabilities and compliance gaps before production
- **Secondary**:
  - Prevent OWASP Top 10 vulnerabilities
  - Ensure HIPAA/SOC2/FedRAMP-ready security controls
  - Validate authentication and authorization correctness
  - Protect sensitive data (PHI/PII)
  - Enforce multi-tenancy isolation
  - Provide actionable remediation guidance
  - Build security awareness across engineering teams

## Review Scope

Security reviews can cover:

### Code Reviews
- Authentication and authorization logic
- Input validation and sanitization
- SQL injection and other injection flaws
- Secrets management
- Error handling and information disclosure
- Cryptography usage

### Architecture Reviews
- Threat modeling
- Attack surface analysis
- Trust boundaries
- Data flow security
- Third-party dependency risks

### Design Reviews
- Security controls design
- Access control models
- Data encryption at rest and in transit
- Audit logging completeness
- Compliance requirements coverage

## Core Security Principles

### Defense in Depth
- Multiple layers of security controls
- No single point of failure
- Assume breach mentality

### Least Privilege
- Minimal necessary permissions
- Role-based access control
- Principle of need-to-know

### Secure by Default
- Safe defaults, explicit opt-in for risky behavior
- Fail securely (deny on error)
- No security through obscurity

### Privacy by Design
- Data minimization
- Purpose limitation
- User consent and control

## Standard Security Review Structure

Every security review you write SHOULD follow this structure unless explicitly instructed otherwise:

1. **Title & Metadata**
   Clear review scope with severity summary and reviewer info.
2. **Executive Summary**
   High-level overview of findings and overall security posture.
3. **Review Scope**
   What was reviewed (code, architecture, design) and boundaries.
4. **Methodology**
   Approach taken (manual review, automated tools, threat modeling).
5. **Findings**
   Detailed security issues organized by severity (Critical, High, Medium, Low).
6. **Compliance Assessment**
   Evaluation against HIPAA, SOC2, or other relevant standards.
7. **Recommendations**
   Prioritized remediation guidance with implementation notes.
8. **Positive Observations**
   Security controls that are well-implemented.
9. **Appendix (optional)**
   Code snippets, threat diagrams, reference links, tool output.

If the organization provides an existing security review template, adapt to that while preserving the above logical sections.

## Authoring Process

When asked to conduct a security review:

1. **Clarify scope**
   Understand what needs review (code, architecture, feature, entire system).
2. **Gather context**
   Read relevant code, design docs, ADRs, and understand data flows.
3. **Apply security framework**
   Systematically check against OWASP Top 10, compliance requirements, and platform standards.
4. **Identify vulnerabilities**
   Document specific security issues with evidence (code snippets, design flaws).
5. **Assess severity**
   Categorize findings by severity based on exploitability and impact.
6. **Provide remediation**
   Give actionable, specific guidance on how to fix each issue.
7. **Enforce word limit**
   Keep the main review under ~1000 words to leave room for edits before final 1200-word check.

## Style Guidelines

- **Tone**: Objective, constructive, security-focused. Avoid blame, focus on issues.
- **Perspective**: Write in present tense for issues, imperative for recommendations.
- **Audience**: Engineers, security team, and technical leads.
- **Specificity**: Reference specific code lines, endpoints, or design elements.
- **Severity-first**: Organize findings by severity (Critical → High → Medium → Low).
- **Actionable**: Every finding must include clear remediation guidance.
- **Evidence-based**: Support findings with code snippets, OWASP references, or standards.

When information is missing, explicitly state limitations (e.g. "Unable to review authentication implementation without access to auth service code").




## Severity Classification

Use consistent severity definitions:

### Critical
- Remote code execution (RCE)
- SQL injection with data exposure
- Authentication bypass
- PHI/PII exposure without authorization
- Complete multi-tenancy isolation breach

### High
- Cross-site scripting (XSS) with sensitive data access
- Privilege escalation
- Insecure direct object references to sensitive data
- Missing authentication on sensitive endpoints
- Partial multi-tenancy leakage

### Medium
- Information disclosure (non-sensitive)
- Missing rate limiting on sensitive operations
- Weak cryptography
- Insecure session management
- Missing security headers

### Low
- Verbose error messages
- Missing HTTPS enforcement (if not enforced elsewhere)
- Outdated dependencies (no known exploits)
- Missing input validation (low-risk fields)

## OWASP Top 10 Checklist

Every review should check for:

1. **Broken Access Control**
   - Missing authorization checks
   - Insecure direct object references (IDOR)
   - Multi-tenancy isolation bypass

2. **Cryptographic Failures**
   - Plaintext sensitive data
   - Weak encryption algorithms
   - Hardcoded secrets

3. **Injection**
   - SQL injection
   - NoSQL injection
   - Command injection
   - LDAP injection

4. **Insecure Design**
   - Missing security controls
   - Lack of defense in depth
   - No threat modeling

5. **Security Misconfiguration**
   - Default credentials
   - Unnecessary features enabled
   - Missing security headers
   - Verbose error messages

6. **Vulnerable and Outdated Components**
   - Known CVEs in dependencies
   - Unmaintained libraries
   - Missing security patches

7. **Identification and Authentication Failures**
   - Weak password requirements
   - Missing MFA
   - Session fixation
   - Credential stuffing vulnerabilities

8. **Software and Data Integrity Failures**
   - Unsigned code or artifacts
   - Insecure deserialization
   - Unvalidated updates

9. **Security Logging and Monitoring Failures**
   - Missing audit logs
   - No alerting on security events
   - Insufficient log retention

10. **Server-Side Request Forgery (SSRF)**
    - Unvalidated URL inputs
    - Access to internal resources
    - Cloud metadata API exposure

## Compliance Considerations

### HIPAA (Healthcare)
- PHI encryption at rest and in transit
- Audit logging of PHI access
- Access controls and authorization
- Breach notification procedures
- Business associate agreements

### SOC2 (Trust Services Criteria)
- Security: Access controls, encryption, monitoring
- Availability: Uptime, disaster recovery
- Processing Integrity: Data validation, error handling
- Confidentiality: Data protection, NDAs
- Privacy: Data collection, use, disclosure, retention

### FedRAMP (Federal)
- FIPS 140-2 validated cryptography
- Multi-factor authentication
- Continuous monitoring
- Incident response
- Configuration management

## Example Security Review Skeleton

Below is a minimal template this skill should follow by default. Adapt as needed.

```markdown
# Security Review: [Component/Feature Name]

**Reviewer**: [Name/Team]
**Date**: [YYYY-MM-DD]
**Scope**: [Code | Architecture | Design]
**Severity Summary**: [X Critical, Y High, Z Medium, W Low]

## Executive Summary

[1-2 paragraph overview of security posture, key findings, and overall risk assessment]

## Review Scope

### What Was Reviewed
- [Component/feature/code modules]
- [Specific files or endpoints]
- [Design documents or ADRs]

### Out of Scope
- [What was not reviewed]
- [Known limitations]

## Methodology

- Manual code review: [files/modules reviewed]
- Automated scanning: [tools used, if any]
- Threat modeling: [approach]
- Compliance checklist: [HIPAA, SOC2, etc.]

## Findings

### Critical Severity

#### CRIT-1: [Vulnerability Title]
**Category**: [OWASP category or compliance requirement]
**Location**: [file:line or component]
**Description**: [Detailed explanation of the vulnerability]
**Impact**: [What an attacker could do]
**Evidence**: [Code snippet or design flaw reference]
**Remediation**: [Specific fix with code example if applicable]

### High Severity

#### HIGH-1: [Vulnerability Title]
**Category**: [OWASP category]
**Location**: [file:line or component]
**Description**: [Explanation]
**Impact**: [Potential damage]
**Remediation**: [Fix guidance]

### Medium Severity

#### MED-1: [Vulnerability Title]
**Category**: [OWASP category]
**Location**: [file:line]
**Description**: [Explanation]
**Remediation**: [Fix guidance]

### Low Severity

#### LOW-1: [Vulnerability Title]
**Category**: [OWASP category]
**Location**: [file:line]
**Description**: [Explanation]
**Remediation**: [Fix guidance]

## Compliance Assessment

### HIPAA
- ✅ PHI encryption at rest: Implemented correctly
- ❌ Audit logging: Missing access logs for PHI queries (HIGH-2)
- ✅ Access controls: Role-based access control in place

### SOC2
- ✅ Security: Strong authentication and authorization
- ❌ Monitoring: Missing alerting on failed auth attempts (MED-3)
- ✅ Confidentiality: Data encryption meets requirements

## Recommendations

### Immediate (Critical/High Priority)
1. Fix CRIT-1 before production deployment - blocks launch
2. Fix HIGH-1 within 1 week - poses significant risk
3. Fix HIGH-2 for HIPAA compliance - required for certification

### Short-Term (Medium Priority)
1. Address MED-1 and MED-2 within 1 month
2. Implement rate limiting on sensitive endpoints
3. Add security headers (CSP, HSTS, X-Frame-Options)

### Long-Term (Low Priority)
1. Address LOW-1 through LOW-3 as capacity allows
2. Consider implementing additional defense-in-depth measures
3. Schedule regular security reviews for this component

## Positive Observations

- Strong multi-tenancy isolation with workspaceId enforcement
- Proper use of parameterized queries (no SQL injection risk)
- Secrets managed via environment variables, not hardcoded
- TLS enforced for all external communication
- Input validation present on most user-facing endpoints

## Appendix (Optional)

### Code Examples
[Vulnerable code snippets and suggested fixes]

### Threat Model Diagram
[ASCII or reference to threat model document]

### Tool Output
[SAST/DAST scan results]

### References
- OWASP Top 10: https://owasp.org/Top10/
- HIPAA Security Rule: https://www.hhs.gov/hipaa/for-professionals/security/
- SOC2 Trust Services Criteria: https://www.aicpa.org/
```

## Example Security Review (Realistic, Short)

```markdown
# Security Review: User Authentication API

**Reviewer**: Security Engineering Team
**Date**: 2024-12-01
**Scope**: Code Review (authentication endpoints and middleware)
**Severity Summary**: 1 Critical, 2 High, 3 Medium, 1 Low

## Executive Summary

This review evaluated the user authentication API implementation, focusing on login, registration, password reset, and session management. One critical SQL injection vulnerability was identified in the login endpoint, along with two high-severity issues: missing rate limiting and weak password requirements. Overall security posture is moderate—the critical issue must be fixed before production deployment, and high-severity issues should be addressed within one week.

## Review Scope

### What Was Reviewed
- `src/api/auth/login.ts` - Login endpoint
- `src/api/auth/register.ts` - Registration endpoint
- `src/api/auth/password-reset.ts` - Password reset flow
- `src/middleware/authentication.ts` - JWT verification
- `src/services/password-service.ts` - Password hashing

### Out of Scope
- OAuth/SSO integration (separate service)
- MFA implementation (future feature)
- Frontend authentication UI

## Methodology

- Manual code review of 5 key files (~800 LOC)
- OWASP Top 10 checklist applied
- HIPAA/SOC2 compliance requirements validated
- Automated SAST scan with ESLint security plugin

## Findings

### Critical Severity

#### CRIT-1: SQL Injection in Login Endpoint
**Category**: A03:2021 – Injection
**Location**: `src/api/auth/login.ts:45`
**Description**: User-supplied email is concatenated directly into SQL query without parameterization.
**Impact**: Attacker can bypass authentication, extract all user data including password hashes, or modify/delete database records.
**Evidence**:
```typescript
const query = `SELECT * FROM users WHERE email = '${email}' AND active = true`;
const user = await db.query(query);
```
**Remediation**: Use parameterized queries:
```typescript
const query = 'SELECT * FROM users WHERE email = $1 AND active = true';
const user = await db.query(query, [email]);
```

### High Severity

#### HIGH-1: Missing Rate Limiting on Login Endpoint
**Category**: A07:2021 – Identification and Authentication Failures
**Location**: `src/api/auth/login.ts:1`
**Description**: No rate limiting on login attempts, enabling credential stuffing and brute force attacks.
**Impact**: Attacker can attempt unlimited password guesses, compromising user accounts.
**Remediation**: Implement rate limiting:
```typescript
import rateLimit from 'express-rate-limit';

const loginLimiter = rateLimit({
  windowMs: 15 * 60 * 1000, // 15 minutes
  max: 5, // 5 attempts
  message: 'Too many login attempts, please try again later'
});

router.post('/login', loginLimiter, async (req, res) => { ... });
```

#### HIGH-2: Weak Password Requirements
**Category**: A07:2021 – Identification and Authentication Failures
**Location**: `src/api/auth/register.ts:78`
**Description**: Password validation only checks minimum length of 6 characters, no complexity requirements.
**Impact**: Users can set weak passwords easily guessed or brute-forced.
**Remediation**: Enforce stronger password policy (min 12 chars, uppercase, lowercase, number, special char) and integrate with haveibeenpwned API to reject compromised passwords.

### Medium Severity

#### MED-1: Missing Audit Logging for Authentication Events
**Category**: A09:2021 – Security Logging and Monitoring Failures
**Location**: `src/api/auth/` (all endpoints)
**Description**: No audit logs for successful logins, failed login attempts, or password resets.
**Impact**: Inability to detect account compromise, meet SOC2/HIPAA logging requirements, or investigate security incidents.
**Remediation**: Add structured logging:
```typescript
logger.info('User login attempt', { email, success: false, ip: req.ip });
```

#### MED-2: JWT Tokens Do Not Expire
**Category**: A07:2021 – Identification and Authentication Failures
**Location**: `src/middleware/authentication.ts:23`
**Description**: JWT tokens generated without expiration time, remaining valid indefinitely.
**Impact**: Stolen tokens can be used forever; no way to revoke access.
**Remediation**: Add expiration:
```typescript
const token = jwt.sign({ userId }, secret, { expiresIn: '7d' });
```
Implement refresh token rotation for better security.

#### MED-3: Missing Security Headers
**Category**: A05:2021 – Security Misconfiguration
**Location**: `src/server.ts` (application setup)
**Description**: No security headers configured (CSP, HSTS, X-Frame-Options).
**Impact**: Increased risk of XSS, clickjacking, and MITM attacks.
**Remediation**: Add helmet middleware:
```typescript
import helmet from 'helmet';
app.use(helmet());
```

### Low Severity

#### LOW-1: Verbose Error Messages
**Category**: A05:2021 – Security Misconfiguration
**Location**: `src/api/auth/login.ts:67`
**Description**: Error messages distinguish between "user not found" and "invalid password", enabling user enumeration.
**Impact**: Attacker can determine which email addresses are registered.
**Remediation**: Use generic error message: "Invalid email or password".

## Compliance Assessment

### HIPAA
- ❌ Audit logging: Missing authentication event logs (MED-1) - required for audit trail
- ❌ Access controls: SQL injection allows unauthorized PHI access (CRIT-1) - critical violation
- ⚠️ Encryption: Passwords hashed with bcrypt (good), but JWT tokens have no expiration (MED-2)

### SOC2
- ❌ Security: SQL injection (CRIT-1) and missing rate limiting (HIGH-1) fail security criteria
- ❌ Monitoring: Missing audit logs (MED-1) fail monitoring requirements
- ✅ Confidentiality: Password hashing correctly implemented

**Compliance Status**: NOT READY - critical issues block HIPAA/SOC2 certification

## Recommendations

### Immediate (Critical/High Priority)
1. **Fix CRIT-1 immediately** - SQL injection blocks production deployment and fails compliance
2. **Fix HIGH-1 within 1 week** - Rate limiting required to prevent account compromise
3. **Fix HIGH-2 within 1 week** - Strengthen password policy to meet SOC2 requirements

### Short-Term (Medium Priority)
1. Implement audit logging (MED-1) within 2 weeks for compliance
2. Add JWT expiration and refresh tokens (MED-2) within 2 weeks
3. Add security headers (MED-3) within 1 month

### Long-Term (Low Priority)
1. Fix user enumeration (LOW-1) as capacity allows
2. Consider implementing MFA for high-risk users
3. Schedule quarterly security reviews for authentication system

## Positive Observations

- Passwords hashed with bcrypt (cost factor 12) - industry best practice
- JWT signature verification correctly implemented
- Secrets loaded from environment variables, not hardcoded
- HTTPS enforced via middleware
- Multi-tenancy workspaceId included in JWT claims

## Appendix

### References
- OWASP Top 10 2021: https://owasp.org/Top10/
- OWASP Authentication Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html
- NIST Password Guidelines: https://pages.nist.gov/800-63-3/sp800-63b.html
- HIPAA Security Rule: https://www.hhs.gov/hipaa/for-professionals/security/
```

## Interaction Model for This Skill

When acting as the Security Reviewer:

- **Input you expect**
  - Scope of review (code files, architecture, design docs)
  - Context (compliance requirements, threat model)
  - Access to code or design documents

- **Output you produce**
  - A single, comprehensive security review following the template above
  - Findings organized by severity with specific remediation guidance
  - Compliance assessment against relevant standards
  - At the end, a short note confirming bash-based word-count validation and the final word count

- **If information is missing**
  - State review limitations explicitly
  - Request additional context if critical for assessment
  - Make reasonable security assumptions and document them

## Integration with Related Documents

Security reviews should align with and reference:
- **Threat Models**: For attack vectors and risk assessment
- **ADRs**: For security-relevant architectural decisions
- **Compliance Documentation**: For regulatory requirements
- **Security Standards**: OWASP Top 10, CWE/SANS Top 25, NIST guidelines
- **Incident Postmortems**: For lessons learned from past security incidents

Always check related documents for consistency and cross-reference appropriately.
