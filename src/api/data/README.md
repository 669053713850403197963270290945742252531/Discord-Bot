# data/

`eff_large_wordlist.txt` -- the EFF "Long Wordlist" for Diceware passphrase
generation (7,776 entries, one per unique 5-digit base-6 dice-roll key), used
by `/genpass`'s Passphrase mode via `api/passwords.py`.

Source: Electronic Frontier Foundation, https://www.eff.org/dice
("Deep Dive: EFF's New Wordlists for Random Passphrases", 2016), licensed
under Creative Commons Attribution 3.0 Unported (CC BY 3.0). Only the word
list itself is reproduced here -- no other content from that page.

Each line is `key<TAB>word`; `api/passwords.load_wordlist()` only reads the
word column and ignores the key.
