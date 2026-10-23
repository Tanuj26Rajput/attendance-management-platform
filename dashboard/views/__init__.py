"""Page bodies.

Named ``views`` rather than ``pages`` on purpose: Streamlit treats a ``pages``
directory beside the entrypoint as an automatic multi-page app and would
publish every module in it as a URL, including the admin ones. Navigation here
is built from the signed-in user's role instead, so a page that an employee
may not see is never routed to.
"""
