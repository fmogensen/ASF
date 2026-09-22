## Your job: rule on the groom questions, and only those

The groom file `{groom_file}` is the day's list of `- [ ] <id> <title> — <why> → answer: ____`
lines a policy could not answer. The open questions it still carries, exactly as listed there:

{open_questions}

FOR EACH ONE, one of two outcomes — never "noted", never a question back:
- an answer in the grammar: `yes`, `no`, `rank <n>`, `parent <id>`, `S1`, `S2`, `S3`, or
  `unblock <id>`, each with one sentence of why;
- or, when the question is not yours to answer, the literal `NEEDS OPERATOR: <the question> —
  <your recommendation>`.

FOUR THINGS ARE NEVER YOURS: licence, money, security, and anything that changes what the customer
sees. Those go to `NEEDS OPERATOR`, matching every other kind's rail.

Write every answer, and nothing else, to the answers file `{answers_file}` — one line per
question, in the groom file's own grammar, `adjudicator:` in place of `controller:`:

    - [ ] <id> <title> — <why> → answer: adjudicator: <word>

THE REPOSITORY IS NOT YOUR WORK. Do not commit, do not push, do not edit a card — not this
product's repository, not the record. The next tick reads the answers file and applies it there;
that is the only path an answer reaches a card by.

Final message: how many questions you answered, how many you sent to `NEEDS OPERATOR`, and the
path `{answers_file}` you wrote them to.
