"""Keep p4a's OpenSSL version and build flags; use verified upstream assets."""
from pythonforandroid.recipes.openssl import OpenSSLRecipe as UpstreamOpenSSLRecipe


class OpenSSLRecipe(UpstreamOpenSSLRecipe):
    version = '3.3.1'
    url = 'https://github.com/openssl/openssl/releases/download/openssl-{version}/openssl-{version}.tar.gz'
    sha256sum = '777cd596284c883375a2a7a11bf5d2786fc5413255efab20c50d6ffe6d020b7e'


recipe = OpenSSLRecipe()
