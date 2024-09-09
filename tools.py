import binascii
import re

def bin2hex(bin):
	return binascii.hexlify(bin).decode('utf-8')

def hex2bin(hx):
	return binascii.unhexlify(re.sub(r'\s', '', hx))