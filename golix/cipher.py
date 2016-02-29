'''
LICENSING
-------------------------------------------------

golix: A python library for Golix protocol object manipulation.
    Copyright (C) 2016 Muterra, Inc.
    
    Contributors
    ------------
    Nick Badger 
        badg@muterra.io | badg@nickbadger.com | nickbadger.com

    This library is free software; you can redistribute it and/or
    modify it under the terms of the GNU Lesser General Public
    License as published by the Free Software Foundation; either
    version 2.1 of the License, or (at your option) any later version.

    This library is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
    Lesser General Public License for more details.

    You should have received a copy of the GNU Lesser General Public
    License along with this library; if not, write to the 
    Free Software Foundation, Inc.,
    51 Franklin Street, 
    Fifth Floor, 
    Boston, MA  02110-1301 USA

------------------------------------------------------

A NOTE ON RANDOM NUMBERS...
PyCryptoDome sources randomness from os.urandom(). This should be secure
for most applications. HOWEVER, if your system is low on entropy (can
be an issue in high-demand applications like servers), urandom *will not
block to wait for entropy*, and will revert (ish?) to potentially 
insufficiently secure pseudorandom generation. In that case, it might be
better to source from elsewhere (like a hardware RNG).

Some initial temporary thoughts:
1. Need to refactor signing, etc into identities.
2. Identity base class should declare supported cipher suites as a set
3. Each identity class should += the set with their support, allowing
    for easy multi-inheritance for multiple identity support
4. Identities then insert the author into the file
5. How does this interact with asymmetric objects with symmetric sigs?
    Should just look for an instance of the object? It would be nice
    to totally factor crypto awareness out of the objects entirely,
    except (of course) for address algorithms.
6. From within python, should the identies be forced to ONLY support
    a single ciphersuite? That would certainly make life easier. A 
    LOT easier. Yeah, let's do that then. Multi-CS identities can
    multi-subclass, and will need to add some kind of glue code for
    key reuse. Deal with that later, but it'll probably entail 
    backwards-incompatible changes.
7. Then, the identities should also generate secrets. That will also
    remove people from screwing up and using ex. random.random().
    But what to do with the API for that? Should identity.finalize(obj)
    return (key, obj) pair or something? That's not going to be useful
    for all objects though, because not all objects use secrets. Really,
    the question is, how to handle GEOCs in a way that makes sense?
    Maybe add an Identity.secrets(guid) attribute or summat? Though
    returning just the bytes would be really unfortunate for app
    development, because you'd have to unpack the generated bytes to
    figure out the guid. What about returning a namedtuple, and adding
    a field for secrets in the GEOC? that might be something to add
    to the actual objects (ex GEOC) instead of the identity. That would
    also reduce the burden on identities for state management of 
    generated objects, which should really be handled at a higher level
    than this library.
8. Algorithm precedence order should be defined globally, but capable
    of being overwritten

Some more temporary thoughts:
1. move all ciphersuite stuff into identities
2. put all of the generic operations like _sign into suite-dependent methods
3. Reference those operations from the first/thirdperson base class
4. Add methods like "create", "bind", "handshake", etc, to identities base
    class, creating the appropriate ex. GEOC objects and returning them,
    potentially along with a guid and (for GEOC specifically) a secret
'''

# Control * imports
__all__ = [
    'AddressAlgo1', 
    'CipherSuite1', 
    'CipherSuite2'
]

# Global dependencies
import io
import struct
import collections
import abc
import json
import base64
import os
from warnings import warn

# import Crypto
# from Crypto.Random import random
from Crypto.Hash import SHA512
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP as OAEP
from Crypto.Signature import pss as PSS
from Crypto.Signature.pss import MGF1
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Util import Counter
from donna25519 import PrivateKey as ECDHPrivate
from donna25519 import PublicKey as ECDHPublic

# Interpackage dependencies
from .utils import Guid
from .utils import SecurityError

from .utils import _dummy_asym
from .utils import _dummy_mac
from .utils import _dummy_signature
from .utils import _dummy_address
from .utils import _dummy_guid
from .utils import _dummy_pubkey
from .utils import ADDRESS_ALGOS
from .utils import Secret

from ._getlow import GIDC
from ._getlow import GEOC
from ._getlow import GOBS
from ._getlow import GOBD
from ._getlow import GDXX
from ._getlow import GARQ

# Some globals
DEFAULT_ADDRESSER = 1
DEFAULT_CIPHER = 1


# Some utilities
class _FrozenHash():
    ''' Somewhat-janky utility PyCryptoDome-specific base class for 
    creating fake hash objects from already-generated hash digests. 
    Looks like a hash, acts like a hash (where appropriate), but doesn't
    carry a state, and all mutability functions are disabled.
    
    On a scale from 1-to-complete-hack, this is probably 2-3 Baja.
    '''
        
    def __init__(self, data):
        if len(data) != self.digest_size:
            raise ValueError('Passed frozen data does not match digest size of hash.')
            
        self._data = data
        
    def update(self, data):
        raise TypeError('Frozen hashes cannot be updated.')
        
    def copy(self):
        raise TypeError('Frozen hashes cannot be copied.')
        
    def digest(self):
        return self._data
    

class _FrozenSHA512(_FrozenHash, SHA512.SHA512Hash):
    pass


class _CipherSuiteBase(metaclass=abc.ABCMeta):
    ''' Abstract base class for all cipher suite objects.
    
    CipherSuite:
        def hasher              (data):
        def signer              (data, private_key):
        def verifier            (data, public_key, signature):
        def public_encryptor    (data, public_key):
        def private_decryptor   (data, private_key):
        def symmetric_encryptor (data, key):
        def symmetric_decryptor (data, key):
    '''    
    @classmethod
    @abc.abstractmethod
    def generate_secret(cls):
        pass
    
    @classmethod
    @abc.abstractmethod
    def hasher(cls, data):
        ''' The hasher used for information addressing.
        '''
        pass
    
    @classmethod
    @abc.abstractmethod
    def signer(cls, private_key, data):
        ''' Placeholder signing method.
        
        Data must be bytes-like. Private key should be a dictionary 
        formatted with all necessary components for a private key (?).
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def verifier(cls, public_key, signature, data):
        ''' Verifies an author's signature against bites. Errors out if 
        unsuccessful. Returns True if successful.
        
        Data must be bytes-like. public_key should be a dictionary 
        formatted with all necessary components for a public key (?).
        Signature must be bytes-like.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def public_encryptor(cls, public_key, data):
        ''' Placeholder asymmetric encryptor.
        
        Data should be bytes-like. Public key should be a dictionary 
        formatted with all necessary components for a public key.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def private_decryptor(cls, private_key, data):
        ''' Placeholder asymmetric decryptor.
        
        Data should be bytes-like. Public key should be a dictionary 
        formatted with all necessary components for a public key.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def symmetric_encryptor(cls, key, data):
        ''' Placeholder symmetric encryptor.
        
        Data should be bytes-like. Key should be bytes-like.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def symmetric_decryptor(cls, key, data):
        ''' Placeholder symmetric decryptor.
        
        Data should be bytes-like. Key should be bytes-like.
        '''
        pass
    
    
class _IdentityBase(metaclass=abc.ABCMeta):
    def __init__(self, keys, author_guid):
        self._author_guid = author_guid
        
        try:
            self._signature_key = keys['signature']
            self._encryption_key = keys['encryption']
            self._exchange_key = keys['exchange']
        except (KeyError, TypeError) as e:
            raise RuntimeError(
                'Generating ID from existing keys requires dict-like obj '
                'with "signature", "encryption", and "exchange" keys.'
            ) from e
    
    @property
    def author_guid(self):
        return self._author_guid
        
    @property
    def ciphersuite(self):
        return self._ciphersuite
        
    @classmethod
    def _dispatch_address(cls, address_algo):
        if address_algo == 'default':
            address_algo = cls.DEFAULT_ADDRESS_ALGO
        elif address_algo not in ADDRESS_ALGOS:
            raise ValueError(
                'Address algorithm unavailable for use: ' + str(address_algo)
            )
        return address_algo
        
    @classmethod
    def _typecheck_secret(cls, secret):
        # Awkward but gets the job done
        if not isinstance(secret, Secret):
            return False
        if secret.cipher != cls._ciphersuite:
            return False
        return True
        
        
class _FirstPersonBase(metaclass=abc.ABCMeta):
    DEFAULT_ADDRESS_ALGO = DEFAULT_ADDRESSER
    
    def __init__(self, keys=None, author_guid=None, address_algo='default', *args, **kwargs):
        self.address_algo = self._dispatch_address(address_algo)
        
        # Load an existing identity
        if keys is not None and author_guid is not None:
            pass
            
        # Catch any improper declaration
        elif keys is not None or author_guid is not None:
            raise TypeError(
                'Generating an ID manually from existing keys requires '
                'both keys and author_guid.'
            )
            
        # Generate a new identity
        else:
            keys = self._generate_keys()
            self._third_party = self._generate_third_person(keys, self.address_algo)
            author_guid = self._third_party.author_guid
            
        # Now dispatch super() with the adjusted keys, author_guid
        super().__init__(keys=keys, author_guid=author_guid, *args, **kwargs)
    
    @property
    def third_party(self):
        return self._third_party
         
    def make_object(self, secret, plaintext):
        if not self._typecheck_secret(secret):
            raise TypeError(
                'Secret must be a properly-formatted Secret compatible with '
                'the current identity\'s declared ciphersuite.'
            )
        
        geoc = GEOC(author=self.author_guid)
        geoc.payload = self._encrypt(secret, plaintext)
        geoc.pack(cipher=self.ciphersuite, address_algo=self.address_algo)
        signature = self._sign(geoc.guid.address)
        geoc.pack_signature(signature)
        # This will need to be converted into a namedtuple or something
        return geoc.guid, geoc.packed
        
    def bind_static(self, guid):        
        gobs = GOBS(
            binder = self.author_guid,
            target = guid
        )
        gobs.pack(cipher=self.ciphersuite, address_algo=self.address_algo)
        signature = self._sign(gobs.guid.address)
        gobs.pack_signature(signature)
        return gobs.guid, gobs.packed
        
    def bind_dynamic(self, guids, address=None, history=None):
        gobd = GOBD(
            binder = self.author_guid,
            targets = guids,
            dynamic_address = address,
            history = history
        )
        gobd.pack(cipher=self.ciphersuite, address_algo=self.address_algo)
        signature = self._sign(gobd.guid.address)
        gobd.pack_signature(signature)
        return gobd.guid, gobd.packed, gobd.dynamic_address
        
    @classmethod
    @abc.abstractmethod
    def _generate_third_person(cls, keys, address_algo):
        ''' MUST ONLY be called when generating one from scratch, not 
        when loading one. Loading must always be done directly through
        loading a ThirdParty.
        '''
        pass
        
    @abc.abstractmethod
    def _generate_keys(self):
        pass
    
    @classmethod
    @abc.abstractmethod
    def new_secret(cls, *args, **kwargs):
        ''' Placeholder method to create new symmetric secret. Returns
        a Secret().
        '''
        return Secret(cipher=cls._ciphersuite, *args, **kwargs)
        
    @abc.abstractmethod
    def _sign(self, data):
        ''' Placeholder signing method.
        '''
        pass
        
    @abc.abstractmethod
    def _decrypt_asym(self, data):
        ''' Placeholder asymmetric decryptor.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def _decrypt(self, secret, data):
        ''' Placeholder symmetric decryptor.
        
        Handle multiple ciphersuites by having a thirdpartyidentity for
        whichever author created it, and calling their decrypt instead.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def _encrypt(self, data):
        ''' Placeholder symmetric encryptor.
        '''
        pass
    
    
class _ThirdPersonBase(metaclass=abc.ABCMeta):
    @classmethod
    def from_keys(cls, keys, address_algo):
        try:
            # Turn them into bytes first.
            packed_keys = cls._pack_keys(keys)
        except (KeyError, TypeError) as e:
            raise RuntimeError(
                'Generating ID from existing keys requires dict-like obj '
                'with "signature", "encryption", and "exchange" keys.'
            ) from e
            
        gidc = GIDC( 
            signature_key=packed_keys['signature'],
            encryption_key=packed_keys['encryption'],
            exchange_key=packed_keys['exchange']
        )
        gidc.pack(cipher=cls._ciphersuite, address_algo=address_algo)
        author_guid = gidc.guid
        self = cls(keys=keys, author_guid=author_guid)
        self.packed = gidc.packed
        return self
    
    def load_geoc(self, secret, geoc):
        # Handle loading a raw geoc, if that's what's passed
        try:
            memoryview(geoc)
            geoc = GEOC.unpack(geoc)
        except TypeError:
            pass
        
        signature = geoc.signature
        self._verify(signature, geoc.guid.address)
        plaintext = self._decrypt(secret, geoc.payload)
        # This will need to be converted into a namedtuple or something
        return geoc.guid, plaintext
        
    @classmethod
    @abc.abstractmethod
    def _pack_keys(cls, keys):
        pass
        
    @abc.abstractmethod
    def _verify(self, signature, data):
        ''' Verifies an author's signature against bites. Errors out if 
        unsuccessful. Returns True if successful.
        '''
        pass
        
    @abc.abstractmethod
    def _encrypt_asym(self, data):
        ''' Placeholder asymmetric encryptor.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def _decrypt(self, secret, data):
        ''' Placeholder symmetric decryptor.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def _encrypt(self, data):
        ''' Placeholder symmetric encryptor.
        '''
        pass
        
        
class CipherSuite0(_CipherSuiteBase):
    ''' FOR TESTING PURPOSES ONLY. 
    
    Entirely inoperative. Correct API, but ignores all input, creating
    only a symbolic output.
    '''
    @classmethod
    def generate_secret(cls):
        return None
    
    @classmethod
    def hasher(cls, *args, **kwargs):
        ''' The hasher used for information addressing.
        '''
        return None
    
    @classmethod
    def signer(cls, *args, **kwargs):
        ''' Placeholder signing method.
        
        Data must be bytes-like. Private key should be a dictionary 
        formatted with all necessary components for a private key (?).
        '''
        return _dummy_signature
        
    @classmethod
    def verifier(cls, *args, **kwargs):
        ''' Verifies an author's signature against bites. Errors out if 
        unsuccessful. Returns True if successful.
        
        Data must be bytes-like. public_key should be a dictionary 
        formatted with all necessary components for a public key (?).
        Signature must be bytes-like.
        '''
        return True
        
    @classmethod
    def public_encryptor(cls, *args, **kwargs):
        ''' Placeholder asymmetric encryptor.
        
        Data should be bytes-like. Public key should be a dictionary 
        formatted with all necessary components for a public key.
        '''
        return _dummy_asym
        
    @classmethod
    def private_decryptor(cls, *args, **kwargs):
        ''' Placeholder asymmetric decryptor.
        
        Maybe add kwarguments do define what kind of internal object is
        returned? That would be smart.
        
        Or, even better, do an arbitrary object content, and then encode
        what class of internal object to use there. That way, it's not
        possible to accidentally encode secrets publicly, but you can 
        also emulate behavior of normal exchange.
        
        Data should be bytes-like. Public key should be a dictionary 
        formatted with all necessary components for a public key.
        '''
        # Note that this will error out when trying to load components,
        # since it's 100% an invalid declaration of internal content.
        # But, it's a good starting point.
        return _dummy_asym
        
    @classmethod
    def symmetric_encryptor(cls, *args, **kwargs):
        ''' Placeholder symmetric encryptor.
        
        Data should be bytes-like. Key should be bytes-like.
        '''
        return b'[[ PLACEHOLDER ENCRYPTED SYMMETRIC MESSAGE. Hello, world? ]]'
        
    @classmethod
    def symmetric_decryptor(cls, *args, **kwargs):
        ''' Placeholder symmetric decryptor.
        
        Data should be bytes-like. Key should be bytes-like.
        '''
        return b'[[ PLACEHOLDER DECRYPTED SYMMETRIC MESSAGE. Hello world! ]]'
        
        
class FirstPersonIdentity0(_FirstPersonBase, _IdentityBase):
    ''' FOR TESTING PURPOSES ONLY. 
    
    Entirely inoperative. Correct API, but ignores all input, creating
    only a symbolic output.
    
    NOTE THAT INHERITANCE ORDER MATTERS! Must be first a FirstPerson, 
    and second an Identity.
    '''
    _ciphersuite = 0
        
    @classmethod
    def _generate_third_person(cls, keys, address_algo):
        keys = {}
        keys['signature'] = _dummy_pubkey
        keys['encryption'] = _dummy_pubkey
        keys['exchange'] = _dummy_pubkey
        return ThirdPersonIdentity0.from_keys(keys, address_algo)
        
    def _generate_keys(self):
        keys = {}
        keys['signature'] = _dummy_pubkey
        keys['encryption'] = _dummy_pubkey
        keys['exchange'] = _dummy_pubkey
        return keys
    
    @classmethod
    def new_secret(cls):
        ''' Placeholder method to create new symmetric secret.
        '''
        return super().new_secret(key=bytes(32), seed=None)
        
    @classmethod
    def _sign(cls, *args, **kwargs):
        ''' Placeholder signing method.
        
        Data must be bytes-like. Private key should be a dictionary 
        formatted with all necessary components for a private key (?).
        '''
        return _dummy_signature
        
    @classmethod
    def _decrypt_asym(cls, *args, **kwargs):
        ''' Placeholder asymmetric decryptor.
        
        Maybe add kwarguments do define what kind of internal object is
        returned? That would be smart.
        
        Or, even better, do an arbitrary object content, and then encode
        what class of internal object to use there. That way, it's not
        possible to accidentally encode secrets publicly, but you can 
        also emulate behavior of normal exchange.
        
        Data should be bytes-like. Public key should be a dictionary 
        formatted with all necessary components for a public key.
        '''
        # Note that this will error out when trying to load components,
        # since it's 100% an invalid declaration of internal content.
        # But, it's a good starting point.
        return _dummy_asym
        
    @classmethod
    def _decrypt(cls, *args, **kwargs):
        ''' Placeholder symmetric decryptor.
        
        Data should be bytes-like. Key should be bytes-like.
        '''
        return b'[[ PLACEHOLDER DECRYPTED SYMMETRIC MESSAGE. Hello world! ]]'
        
    @classmethod
    def _encrypt(cls, *args, **kwargs):
        ''' Placeholder symmetric encryptor.
        
        Data should be bytes-like. Key should be bytes-like.
        '''
        return b'[[ PLACEHOLDER ENCRYPTED SYMMETRIC MESSAGE. Hello, world? ]]'
    
        
class ThirdPersonIdentity0(_ThirdPersonBase, _IdentityBase):
    _ciphersuite = 0
        
    @classmethod
    def _pack_keys(cls, keys):
        return keys
        
    @classmethod
    def _verify(cls, *args, **kwargs):
        ''' Verifies an author's signature against bites. Errors out if 
        unsuccessful. Returns True if successful.
        
        Data must be bytes-like. public_key should be a dictionary 
        formatted with all necessary components for a public key (?).
        Signature must be bytes-like.
        '''
        return True
        
    @classmethod
    def _encrypt_asym(cls, *args, **kwargs):
        ''' Placeholder asymmetric encryptor.
        
        Data should be bytes-like. Public key should be a dictionary 
        formatted with all necessary components for a public key.
        '''
        return _dummy_asym
        
    # Well it's not exactly repeating yourself, though it does mean there
    # are sorta two ways to perform decryption. Best practice = always decrypt
    # using the author's ThirdPersonIdentity
    _encrypt = FirstPersonIdentity0._encrypt
    _decrypt = FirstPersonIdentity0._decrypt


class CipherSuite1(_CipherSuiteBase):
    ''' SHA512, AES256-SIV, RSA-4096, ECDH-C25519
    
    Generic, all-static-method class for cipher suite #1.
    '''
    # Signature constants.
    # Put these here because 1. explicit and 2. what if PCD API changes?
    # Explicit is better than implicit!
    HASH_ALGO = SHA512
    PSS_MGF = lambda x, y: MGF1(x, y, SHA512)
    PSS_SALT_LENGTH = SHA512.digest_size
    # example calls:
    # h = _FrozenSHA512(data)
    # pss.new(private_key, mask_func=PSS_MGF, salt_bytes=PSS_SALT_LENGTH).sign(h)
    # or, on the receiving end:
    # pss.new(private_key, mask_func=PSS_MGF, salt_bytes=PSS_SALT_LENGTH).verify(h, signature)
    # Verification returns nothing (=None) if successful, raises ValueError if not
    
    @classmethod
    def hasher(cls, data):
        ''' Man, this bytes.'''
        h = cls.HASH_ALGO.new(data)
        # Give it the bytes
        h.update(data)
        digest = bytes(h.digest())
        # So this isn't really making much of a difference, necessarily, but
        # it's good insurance against (accidental or malicious) length
        # extension problems.
        del h
        return digest
        
    @classmethod
    def signer(cls, private_key, data):
        pre_tuple = []
        
        # Extract the needed values and construct the private key
        pre_tuple.append(private_key['modulus']) # n
        pre_tuple.append(private_key['publicExponent']) # e
        pre_tuple.append(private_key['privateExponent']) # d
        # If missing primes, recover them.
        try:
            pre_tuple.extend(None, None)
            pre_tuple[3] = private_key['prime1']
            pre_tuple[4] = private_key['prime2']
        except KeyError:
            del pre_tuple[3], pre_tuple[4]
        # Should add CRT coefficient U in here, but maybe later
        if len(pre_tuple) > 3:
            try:
                pre_tuple.append(private_key['keyerror'])
            except KeyError:
                pass
        
        # Now generate the key.
        key = rsa2.construct(pre_tuple)
        
        # rsa2 stuff follows
        # key = rsa2.construct((n, e, d, p, q, u))
        # Build the signer using the MGF1 SHA512
        # eh, do this shit later
        # signer = PKCS1_PSS.new(key, MGF1_SHA512)
        # DOES PKCS1_PSS HASH INTERNALLY??
        
        # FILE MARKER
        # LEFT OFF HERE
        # OTHER SEARCH TERMS
        # HELLO
        # HASHTAG BADGER
        # Okay but seriously, this presents something of a problem that I'm not
        # entirely sure how to handle. In the future there may be multiple hashes -- 
        # I mean is the future really even relevant? -- but in the future there might
        # be multiple hashes and that would create a problem. Because this library doesn't
        # distinguish between signing hashes and signing data. It generates its own hash.
        # Wait a second, is that right? Is it actually hashing the data, or just using
        # the hash as a hash function generator? I mean, I'd assume the former, buuuut...
        
        digest = cls.hasher()
        
        signer = key.signer(
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA512()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA512()
        )
        signer.update(data)
        signature = signer.finalize()
        del signer, key, data, private_key, n, e, d, p, q, dmp1, dmq1, iqmp
        
        return signature

    @classmethod
    def verifier(cls, public_key, signature, data):
        ''' Verifies an author's signature against bites. Errors out if 
        unsuccessful. Returns True if successful.
        '''
        # pseudocode: return rsa2048verify(bites, pubkey, signature)
        # Extract needed values from the dictionary and create a public key
        n = public_key['modulus']
        e = public_key['publicExponent']
        pubkey = rsa.RSAPublicNumbers(e, n).public_key(cls.BACKEND)
        # Create the verifier
        verifier = pubkey.verifier(
            signature,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA512()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA512()
        )
        verifier.update(data)
        verifier.verify()
        # That will error out if bad. Might want to provide a custom, crypto
        # library-independed error to raise.
        
        # Success!
        return True

    @classmethod
    def public_encryptor(cls, public_key, data):
        # Extract needed values from the dictionary and create a public key
        n = public_key['modulus']
        e = public_key['publicExponent']
        pubkey = rsa2.construct((n, e))
        # Encrypt
        cipher = PKCS1_OAEP.new(pubkey, hashAlgo=SHA512)
        return cipher.encrypt(data)

    @classmethod
    def private_decryptor(cls, private_key, data):
        ''' Implements the EICa standard to unencrypt the payload, then 
        immediately deletes the key.
        '''
        # These will always be present in the private_key. Not currently
        # bothering with the rest.
        n = private_key['modulus']
        e = private_key['publicExponent']
        d = private_key['privateExponent']
        q = private_key['prime1']
        p = private_key['prime2']
        iqmp = private_key['iqmp']
        
        # Construct the key.
        key = rsa2.construct((n, e, d, p, q, iqmp))
        
        cipher = PKCS1_OAEP.new(key, hashAlgo=SHA512)
        plaintext = cipher.decrypt(data)
        del private_key, cipher, n, e, d, key
        return plaintext
        
    @classmethod
    def symmetric_encryptor(cls, key, data):
        ''' Performs symmetric encryption of the supplied payload using 
        the supplied symmetric key.
        '''
        #self.check_symkey(sym_key)
        nonce = os.urandom(16)
        cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), 
                        backend=cls.BACKEND)
        del key
        encryptor = cipher.encryptor()
        # Note that update returns value immediately, but finalize should (at 
        # least in CTR mode) return nothing.
        ct = encryptor.update(data) + encryptor.finalize()
        # Don't forget to prepend the nonce
        payload = nonce + ct
        # Delete these guys for some reassurrance
        del data, cipher, encryptor, nonce, ct
        return payload

    @classmethod
    def symmetric_decryptor(cls, key, data):
        ''' Performs symmetric decryption of the supplied payload using
        the supplied symmetric key.
        '''
        nonce = data[0:16]
        payload = data[16:]
        cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), 
                        backend=cls.BACKEND)
        del key
        decryptor = cipher.decryptor()
        payload = decryptor.update(payload) + decryptor.finalize()
        del decryptor, cipher, nonce
        return payload


# Signature constants.
# Put these here because 1. explicit and 2. what if PCD API changes?
# Explicit is better than implicit!
# Don't include these in the class 1. to avoid cluttering it and 2. to avoid
# accidentally passing self
_PSS_SALT_LENGTH = SHA512.digest_size
_PSS_MGF = lambda x, y: MGF1(x, y, SHA512)
# example calls:
# h = _FrozenSHA512(data)
# PSS.new(private_key, mask_func=PSS_MGF, salt_bytes=PSS_SALT_LENGTH).sign(h)
# or, on the receiving end:
# PSS.new(public_key, mask_func=PSS_MGF, salt_bytes=PSS_SALT_LENGTH).verify(h, signature)
# Verification returns nothing (=None) if successful, raises ValueError if not
class FirstPersonIdentity1(_FirstPersonBase, _IdentityBase):
    ''' ... Hmmm
    '''
    _ciphersuite = 1
        
    @classmethod
    def _generate_third_person(cls, keys, address_algo):
        pubkeys = {
            'signature': keys['signature'].publickey(),
            'encryption': keys['encryption'].publickey(),
            'exchange': keys['exchange'].get_public()
        } 
        del keys
        return ThirdPersonIdentity1.from_keys(keys=pubkeys, address_algo=address_algo)
        
    @classmethod
    def _generate_keys(cls):
        keys = {}
        keys['signature'] = RSA.generate(4096)
        keys['encryption'] = RSA.generate(4096)
        keys['exchange'] = ECDHPrivate()
        return keys
    
    @classmethod
    def new_secret(cls):
        ''' Returns a new secure Secret().
        '''
        key = get_random_bytes(32)
        nonce = get_random_bytes(16)
        return super().new_secret(key=key, seed=nonce)
        
    def _sign(self, data):
        ''' Placeholder signing method.
        '''
        h = _FrozenSHA512(data)
        signer = PSS.new(
            self._signature_key, 
            mask_func=_PSS_MGF, 
            salt_bytes=_PSS_SALT_LENGTH
        )
        return signer.sign(h)
        
    def _decrypt_asym(self, data):
        ''' Placeholder asymmetric decryptor.
        '''
        pass
        
    @classmethod
    def _decrypt(self, secret, data):
        ''' Placeholder symmetric decryptor.
        
        Handle multiple ciphersuites by having a thirdpartyidentity for
        whichever author created it, and calling their decrypt instead.
        '''
        # Courtesy of pycryptodome's API limitations:
        if not isinstance(data, bytes):
            data = bytes(data)
        # Convert the secret's seed (nonce) into an integer for pycryptodome
        ctr_start = int.from_bytes(secret.seed, byteorder='big')
        ctr = Counter.new(nbits=128, initial_value=ctr_start)
        cipher = AES.new(key=secret.key, mode=AES.MODE_CTR, counter=ctr)
        return cipher.decrypt(data)
        
    @classmethod
    def _encrypt(self, secret, data):
        ''' Symmetric encryptor.
        '''
        # Courtesy of pycryptodome's API limitations:
        if not isinstance(data, bytes):
            data = bytes(data)
        # Convert the secret's seed (nonce) into an integer for pycryptodome
        ctr_start = int.from_bytes(secret.seed, byteorder='big')
        ctr = Counter.new(nbits=128, initial_value=ctr_start)
        cipher = AES.new(key=secret.key, mode=AES.MODE_CTR, counter=ctr)
        return cipher.encrypt(data)
        

class ThirdPersonIdentity1(_ThirdPersonBase, _IdentityBase): 
    _ciphersuite = 1  
        
    @classmethod
    def _pack_keys(cls, keys):
        packkeys = {
            'signature': int.to_bytes(keys['signature'].n, length=512, byteorder='big'),
            'encryption': int.to_bytes(keys['encryption'].n, length=512, byteorder='big'),
            'exchange': keys['exchange'].public,
        }
        return packkeys
       
    def _verify(self, signature, data):
        ''' Verifies an author's signature against bites. Errors out if 
        unsuccessful. Returns True if successful.
        
        Data must be bytes-like. public_key should be a dictionary 
        formatted with all necessary components for a public key (?).
        Signature must be bytes-like.
        '''
        h = _FrozenSHA512(data)
        signer = PSS.new(self._signature_key, mask_func=_PSS_MGF, salt_bytes=_PSS_SALT_LENGTH)
        try:
            signer.verify(h, signature)
        except ValueError as e:
            raise SecurityError('Failed to verify signature.') from e
            
        return True
        
    @classmethod
    def _encrypt_asym(cls, *args, **kwargs):
        ''' Placeholder asymmetric encryptor.
        
        Data should be bytes-like. Public key should be a dictionary 
        formatted with all necessary components for a public key.
        '''
        return _dummy_asym
        
    # Well it's not exactly repeating yourself, though it does mean there
    # are sorta two ways to perform decryption. Best practice = always decrypt
    # using the author's ThirdPersonIdentity
    _encrypt = FirstPersonIdentity1._encrypt
    _decrypt = FirstPersonIdentity1._decrypt
    
  
# Zero should be rendered inop, IE ignore all input data and generate
# symbolic representations
CIPHER_SUITES = {
    0: CipherSuite0,
    1: CipherSuite1,
}

def cipher_lookup(num):
    try:
        return CIPHER_SUITES[num]
    except KeyError as e:
        raise ValueError('Cipher suite "' + str(num) + '" is undefined.') from e