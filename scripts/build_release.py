"""Compatibility entry for the sanitized complete public-source/plugin builder.

Do not recursively archive docs, temporary directories or installed user data.
The public builder includes corresponding source, a matched wheel and notices.
"""
from build_plugin_release import main

if __name__=='__main__':main()
