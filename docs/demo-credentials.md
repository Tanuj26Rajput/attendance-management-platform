# Demo credentials — development only

> **These accounts are for local demos and assessment walkthroughs only.**
> They are fictional people on the reserved `example.com` domain, and their
> passwords are published in this file, which means they are public. **Do not
> seed them into any shared, staging or production database.** If you deploy
> this platform anywhere reachable, delete or disable every account below and
> change any password you reused, before you expose it.

The seeder refuses to run unless `APP_ENV` is `local`, `dev` or `test`, and it
refuses again unless you pass `--yes`. See
[Seeding demo data](../README.md#seeding-demo-data).

Passwords are stored only as Argon2id hashes, produced by the application's own
`app.core.security.hash_password` — the seeder does not write plaintext to the
database and does not weaken the hashing parameters.

## Accounts

| Name | Email | Password | Role | Department | Shift |
|---|---|---|---|---|---|
| Ada Whitfield | `ada.whitfield@example.com` | `Demo!Admin-7fQ2vXpL` | **admin** | People Operations | General (IST) |
| Marcus Delacroix | `marcus.delacroix@example.com` | `Demo!Lead-4hT9zRmK` | employee *(dept. head)* | Engineering | General (IST) |
| Priya Raghunathan | `priya.raghunathan@example.com` | `Demo!Lead-6bW3nYcD` | employee *(dept. head)* | Sales | General (IST) |
| Tomas Lindqvist | `tomas.lindqvist@example.com` | `Demo!Staff-2kP8sVnR` | employee | Engineering | General (IST) |
| Chidinma Okonkwo | `chidinma.okonkwo@example.com` | `Demo!Staff-9xL4jHtB` | employee | Engineering | Europe Day (London) |
| Rafael Monteiro | `rafael.monteiro@example.com` | `Demo!Staff-5nG7qZwM` | employee | Engineering | Night Support (overnight, UTC) |
| Hana Sugiyama | `hana.sugiyama@example.com` | `Demo!Staff-3cR6dFyT` | employee | Sales | General (IST) |
| Oliver Brennan | `oliver.brennan@example.com` | `Demo!Staff-8vJ2mQsN` | employee | Sales | Europe Day (London) |
| Noor Al-Hassani | `noor.alhassani@example.com` | `Demo!Staff-1zK5tBpW` | employee | Customer Support | Night Support (overnight, UTC) |
| Gabriel Osei | `gabriel.osei@example.com` | `Demo!Staff-7yD3wLxC` | employee | Customer Support | Weekend Support (Sat–Sun, IST) |
| Elena Castellanos | `elena.castellanos@example.com` | `Demo!Staff-4qN9rGvH` | employee | People Operations | General (IST) |

Eleven accounts: one administrator, two department heads, eight individual
contributors, across four departments and four shifts.

## A note on the two "managers"

**This platform has no manager role.** `UserRole` has exactly two members,
`employee` and `admin`, and every privileged route checks for `admin`. Rather
than invent a role — which would mean changing the authorisation model to make
a demo look better — the two department managers are seeded as the only thing
the schema actually models:

- they are set as `departments.head_id` for Engineering and Sales, and
- their reports' `users.manager_id` points at them.

That gives them a real reporting line and a real place in the org chart, and
**no extra permissions**: signed in, Marcus and Priya see the same employee
dashboard as anyone else. To demonstrate the admin dashboard — directory,
reports, analytics, audit trail — sign in as Ada Whitfield.

## Rotating these passwords

Every run of the seeder re-hashes the passwords above for these eleven accounts
so this file can never drift out of date. No account outside this roster is
touched. To use different passwords, edit `PEOPLE` in `app/seed/demo.py`,
update this table, and re-run the seeder.
