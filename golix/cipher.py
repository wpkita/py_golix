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
'''

# Control * imports
__all__ = [
    'FirstParty1', 
    'SecondParty1', 
    'ThirdParty1'
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
from Crypto.Protocol.KDF import HKDF
from Crypto.Hash import HMAC
from donna25519 import PrivateKey as ECDHPrivate
from donna25519 import PublicKey as ECDHPublic

from smartyparse import ParseError

# Interpackage dependencies
from .utils import Guid
from .utils import SecurityError
from .utils import ADDRESS_ALGOS
from .utils import Secret

from .utils import AsymHandshake
from .utils import AsymAck
from .utils import AsymNak

from .utils import _dummy_asym
from .utils import _dummy_mac
from .utils import _dummy_signature
from .utils import _dummy_address
from .utils import _dummy_guid
from .utils import _dummy_pubkey

from ._getlow import GIDC
from ._getlow import GEOC
from ._getlow import GOBS
from ._getlow import GOBD
from ._getlow import GDXX
from ._getlow import GARQ

from ._getlow import GARQHandshake
from ._getlow import GARQAck
from ._getlow import GARQNak

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
    
    
class _IdentityBase(metaclass=abc.ABCMeta):
    def __init__(self, keys, guid):
        self._guid = guid
        
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
    def guid(self):
        return self._guid
        
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
        
        
class _ObjectHandlerBase():
    ''' Base class for anything that needs to unpack Golix objects.
    '''
    @staticmethod
    def unpack_identity(packed):
        gidc = GIDC.unpack(packed)
        return gidc
    
    @staticmethod
    def unpack_container(packed):
        geoc = GEOC.unpack(packed)
        return geoc
        
    @staticmethod
    def unpack_bind_static(packed):
        gobs = GOBS.unpack(packed)
        return gobs
        
    @staticmethod
    def unpack_bind_dynamic(packed):
        gobd = GOBD.unpack(packed)
        return gobd
        
    @staticmethod
    def unpack_debind(packed):
        gdxx = GDXX.unpack(packed)
        return gdxx
    
    
class _SecondPartyBase(metaclass=abc.ABCMeta):
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
        guid = gidc.guid
        self = cls(keys=keys, guid=guid)
        self.packed = gidc.packed
        return self
        
    @classmethod
    def from_identity(cls, gidc):
        ''' Loads an unpacked gidc into a SecondParty. Note that this 
        does not select the correct SecondParty for any given gidc's 
        ciphersuite.
        '''
        guid = gidc.guid
        keys = {
            'signature': gidc.signature_key,
            'encryption': gidc.encryption_key,
            'exchange': gidc.exchange_key
        }
        self = cls(keys=keys, guid=guid)
        return self
        
    @classmethod
    def from_packed(cls, packed):
        ''' Loads a packed gidc into a SecondParty. Also does not select
        the correct SecondParty for the packed gidc's ciphersuite.
        '''
        gidc = _ObjectHandlerBase.unpack_identity(packed)
        self = cls.from_identity(gidc)
        self.packed = packed
        return self
        
    @classmethod
    @abc.abstractmethod
    def _pack_keys(cls, keys):
        ''' Convert self.keys from objects used for crypto operations
        into bytes-like objects suitable for output into a GIDC.
        '''
        pass
        
        
class _FirstPartyBase(_ObjectHandlerBase, metaclass=abc.ABCMeta):
    DEFAULT_ADDRESS_ALGO = DEFAULT_ADDRESSER
    
    def __init__(self, keys=None, guid=None, address_algo='default', *args, **kwargs):
        self.address_algo = self._dispatch_address(address_algo)
        
        # Load an existing identity
        if keys is not None and guid is not None:
            pass
            
        # Catch any improper declaration
        elif keys is not None or guid is not None:
            raise TypeError(
                'Generating an ID manually from existing keys requires '
                'both keys and guid.'
            )
            
        # Generate a new identity
        else:
            keys = self._generate_keys()
            self._second_party = self._generate_second_party(keys, self.address_algo)
            guid = self._second_party.guid
            
        # Now dispatch super() with the adjusted keys, guid
        super().__init__(keys=keys, guid=guid, *args, **kwargs)
        
    @classmethod
    def _typecheck_2ndparty(cls, obj):
        # Type check the partner. Must be SecondPartyX or similar.
        if not isinstance(obj, cls._2PID):
            raise TypeError(
                'Object must be a SecondParty of compatible type '
                'with the FirstParty initiating the request/ack/nak.'
            )
        else:
            return True
    
    @property
    def second_party(self):
        return self._second_party
         
    def make_container(self, secret, plaintext):
        if not self._typecheck_secret(secret):
            raise TypeError(
                'Secret must be a properly-formatted Secret compatible with '
                'the current identity\'s declared ciphersuite.'
            )
        
        geoc = GEOC(author=self.guid)
        geoc.payload = self._encrypt(secret, plaintext)
        geoc.pack(cipher=self.ciphersuite, address_algo=self.address_algo)
        signature = self._sign(geoc.guid.address)
        geoc.pack_signature(signature)
        return geoc
        
    def make_bind_static(self, target):        
        gobs = GOBS(
            binder = self.guid,
            target = target
        )
        gobs.pack(cipher=self.ciphersuite, address_algo=self.address_algo)
        signature = self._sign(gobs.guid.address)
        gobs.pack_signature(signature)
        return gobs
        
    def make_bind_dynamic(self, target, guid_dynamic=None, history=None):
        gobd = GOBD(
            binder = self.guid,
            target = target,
            guid_dynamic = guid_dynamic,
            history = history
        )
        gobd.pack(cipher=self.ciphersuite, address_algo=self.address_algo)
        signature = self._sign(gobd.guid.address)
        gobd.pack_signature(signature)
        return gobd
        
    def make_debind(self, target):
        gdxx = GDXX(
            debinder = self.guid,
            target = target
        )
        gdxx.pack(cipher=self.ciphersuite, address_algo=self.address_algo)
        signature = self._sign(gdxx.guid.address)
        gdxx.pack_signature(signature)
        return gdxx
        
    def make_handshake(self, secret, target):
        return AsymHandshake(
            author = self.guid,
            target = target,
            secret = secret
        )
        
    def make_ack(self, target, status=0):
        return AsymAck(
            author = self.guid,
            target = target,
            status = status
        )
        
    def make_nak(self, target, status=0):
        return AsymNak(
            author = self.guid,
            target = target,
            status = status
        )
        
    def make_request(self, recipient, request):
        self._typecheck_2ndparty(recipient)
        
        # I'm actually okay with this performance hit, since it forces some
        # level of type checking here. Which is, I think, in this case, good.
        if isinstance(request, AsymHandshake):
            request = GARQHandshake(
                author = request.author,
                target = request.target,
                secret = request.secret
            )
        elif isinstance(request, AsymAck):
            request = GARQAck(
                author = request.author,
                target = request.target,
                status = request.status
            )
        elif isinstance(request, AsymNak):
            request = GARQNak(
                author = request.author,
                target = request.target,
                status = request.status
            )
        else:
            raise TypeError(
                'Request must be an AsymHandshake, AsymAck, or AsymNak '
                '(or subclass thereof).'
            )
        request.pack()
        plaintext = request.packed
        
        # Convert the plaintext to a proper payload and create a garq from it
        payload = self._encrypt_asym(recipient, plaintext)
        del plaintext
        garq = GARQ(
            recipient = recipient.guid,
            payload = payload
        )
        
        # Pack 'er up and generate a MAC for it
        garq.pack(cipher=self.ciphersuite, address_algo=self.address_algo)
        garq.pack_signature(
            self._mac(
                key = self._derive_shared(recipient),
                data = garq.guid.address
            )
        )
        
        return garq
    
    def receive_container(self, author, secret, container):
        if not isinstance(container, GEOC):
            raise TypeError(
                'Container must be an unpacked GEOC, for example, as returned '
                'from unpack_container.'
            )
        self._typecheck_2ndparty(author)
        
        signature = container.signature
        self._verify(author, signature, container.guid.address)
        plaintext = self._decrypt(secret, container.payload)
        # This will need to be converted into a namedtuple or something
        return plaintext
    
    def receive_bind_static(self, binder, binding):
        if not isinstance(binding, GOBS):
            raise TypeError(
                'Binding must be an unpacked GOBS, for example, as returned '
                'from unpack_bind_static.'
            )
        self._typecheck_2ndparty(binder)
        
        signature = binding.signature
        self._verify(binder, signature, binding.guid.address)
        # This will need to be converted into a namedtuple or something
        return binding.target
    
    def receive_bind_dynamic(self, binder, binding):
        if not isinstance(binding, GOBD):
            raise TypeError(
                'Binding must be an unpacked GOBD, for example, as returned '
                'from unpack_bind_dynamic.'
            )
        self._typecheck_2ndparty(binder)
        
        signature = binding.signature
        self._verify(binder, signature, binding.guid.address)
        # This will need to be converted into a namedtuple or something
        return binding.target
    
    def receive_debind(self, debinder, debinding):
        if not isinstance(debinding, GDXX):
            raise TypeError(
                'Debinding must be an unpacked GDXX, for example, as returned '
                'from unpack_debind.'
            )
        self._typecheck_2ndparty(debinder)
        
        signature = debinding.signature
        self._verify(debinder, signature, debinding.guid.address)
        # This will need to be converted into a namedtuple or something
        return debinding.target
        
    def unpack_request(self, packed):
        garq = GARQ.unpack(packed)
        plaintext = self._decrypt_asym(garq.payload)
        
        # Try all object handlers available for asymmetric payloads
        parse_success = False
        # Could do this with a loop, but it gets awkward when trying to
        # assign stuff to the resulting object.
        try:
            unpacked = GARQHandshake.unpack(plaintext)
            request = AsymHandshake(
                author = unpacked.author,
                target = unpacked.target, 
                secret = unpacked.secret
            )
        except ParseError:
            try:
                unpacked = GARQAck.unpack(plaintext)
                request = AsymAck(
                    author = unpacked.author,
                    target = unpacked.target, 
                    status = unpacked.status
                )
            except ParseError:
                try:
                    unpacked = GARQNak.unpack(plaintext)
                    request = AsymNak(
                        author = unpacked.author,
                        target = unpacked.target, 
                        status = unpacked.status
                    )
                except ParseError:
                    raise SecurityError('Could not securely unpack request.')
            
        garq._plaintext = request
        garq._author = request.author
        
        return garq
        
    def receive_request(self, requestor, request):
        ''' Verifies the request and exposes its contents.
        '''
        # Typecheck all the things
        self._typecheck_2ndparty(requestor)
        # Also make sure the request is something we've already unpacked
        if not isinstance(request, GARQ):
            raise TypeError(
                'Request must be an unpacked GARQ, as returned from '
                'unpack_request.'
            )
        try:
            plaintext = request._plaintext
        except AttributeError as e:
            raise TypeError(
                'Request must be an unpacked GARQ, as returned from '
                'unpack_request.'
            ) from e
            
        self._verify_mac(
            key = self._derive_shared(requestor),
            data = request.guid.address,
            mac = request.signature
        )
        
        del request._plaintext, request.author
        return plaintext
        
    @classmethod
    @abc.abstractmethod
    def _generate_second_party(cls, keys, address_algo):
        ''' MUST ONLY be called when generating one from scratch, not 
        when loading one. Loading must always be done directly through
        loading a SecondParty.
        '''
        pass
        
    @abc.abstractmethod
    def _generate_keys(self):
        ''' Create a set of keys for use in the identity.
        
        Must return a mapping of keys with the following values:
        {
            'signature': <signature key>,
            'encryption': <encryption key>,
            'exchange': <exchange key>
        }
        In a form that is usable by the rest of the FirstParty
        crypto functions (this is dependent on the individual class' 
        implementation, ex its crypto library).
        '''
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
    def _verify(self, public, signature, data):
        ''' Verifies signature against data using SecondParty public.
        
        raises SecurityError if verification fails.
        returns True on success.
        '''
        pass
        
    @abc.abstractmethod
    def _encrypt_asym(self, public, data):
        ''' Placeholder asymmetric encryptor.
        '''
        pass
        
    @abc.abstractmethod
    def _decrypt_asym(self, data):
        ''' Placeholder asymmetric decryptor.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def _decrypt(cls, secret, data):
        ''' Placeholder symmetric decryptor.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def _encrypt(cls, secret, data):
        ''' Placeholder symmetric encryptor.
        '''
        pass
        
    @abc.abstractmethod
    def _derive_shared(self, partner):
        ''' Derive a shared secret (not necessarily a Secret!) with the 
        partner.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def _mac(cls, key, data):
        ''' Generate a MAC for data using key.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def _verify_mac(cls, key, mac, data):
        ''' Generate a MAC for data using key.
        '''
        pass
        
    @abc.abstractmethod
    def _serialize(self):
        ''' Convert private keys into a standardized format. Don't save,
        just return a dictionary with bytes objects:
        
        {
            'guid': self.guid,
            'signature': self._signature_key,
            'encryption': self._encryption_key,
            'exchange': self._exchange_key
        }
        (etc)
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def _from_serialized(cls, serialization):
        ''' Create an instance of the class from a dictionary as created
        by cls._serialize.
        '''
        pass
        
        
class _ThirdPartyBase(_ObjectHandlerBase, metaclass=abc.ABCMeta):
    ''' Subclass this (on a per-ciphersuite basis) for servers, and 
    other parties that have no access to privileged information. 
    They can only verify.
    '''
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
        
    @staticmethod
    def unpack_object(packed):
        ''' Unpacks any Golix object.
        '''
        success = False
        for golix_format in (GIDC, GEOC, GOBS, GOBD, GDXX, GARQ):
            try:
                obj = golix_format.unpack(packed)
                success = True
            except ParseError:
                pass
        if not success:
            raise ParseError(
                'Packed data does not appear to be a Golix object.'
            )
        return obj
        
    @classmethod
    def unpack_request(cls, packed):
        ''' Unpack public everything from a request.
        (Cannot verify, at least for the existing ciphersuites, as of
        2016-03).
        '''
        garq = GARQ.unpack(packed)
        return garq
        
    @classmethod
    def verify_object(cls, second_party, obj):
        ''' Verifies the signature of any symmetric object (aka 
        everything except GARQ) against data.
        
        raises TypeError if obj is an asymmetric object (or otherwise 
            unsupported).
        raises SecurityError if verification fails.
        returns True on success.
        '''
        if isinstance(obj, GEOC) or \
            isinstance(obj, GOBS) or \
            isinstance(obj, GOBD) or \
            isinstance(obj, GDXX):
                return cls._verify(
                    public = second_party, 
                    signature = obj.signature,
                    data = obj.guid.address
                )
        elif isinstance(obj, GARQ):
            raise ValueError(
                'Asymmetric objects cannot be verified by third parties. '
                'They can only be verified by their recipients.'
            )
        elif isinstance(obj, GIDC):
            raise ValueError(
                'Identity containers are inherently un-verified.'
            )
        else:
            raise TypeError('Obj must be a Golix object: GIDC, GEOC, etc.')
            
    @classmethod
    @abc.abstractmethod
    def _verify(cls, public, signature, data):
        ''' Verifies signature against data using SecondParty public.
        
        raises SecurityError if verification fails.
        returns True on success.
        '''
        pass
    
        
class SecondParty0(_SecondPartyBase, _IdentityBase):
    _ciphersuite = 0
        
    @classmethod
    def _pack_keys(cls, keys):
        return keys
        
        
class FirstParty0(_FirstPartyBase, _IdentityBase):
    ''' FOR TESTING PURPOSES ONLY. 
    
    Entirely inoperative. Correct API, but ignores all input, creating
    only a symbolic output.
    
    NOTE THAT INHERITANCE ORDER MATTERS! Must be first a FirstParty, 
    and second an Identity.
    '''
    _ciphersuite = 0
    _2PID = SecondParty0
        
    # Well it's not exactly repeating yourself, though it does mean there
    # are sorta two ways to perform decryption. Best practice = always decrypt
    # using the author's SecondParty
        
    @classmethod
    def _generate_second_party(cls, keys, address_algo):
        keys = {}
        keys['signature'] = _dummy_pubkey
        keys['encryption'] = _dummy_pubkey
        keys['exchange'] = _dummy_pubkey
        return cls._2PID.from_keys(keys, address_algo)
        
    def _generate_keys(self):
        keys = {}
        keys['signature'] = _dummy_pubkey
        keys['encryption'] = _dummy_pubkey
        keys['exchange'] = _dummy_pubkey
        return keys
        
    def _serialize(self):
        return {
            'guid': bytes(self.guid),
            'signature': self._signature_key,
            'encryption': self._encryption_key,
            'exchange': self._exchange_key
        }
        
    @classmethod
    def _from_serialized(cls, serialization):
        try:
            guid = Guid.from_bytes(serialization['guid'])
            keys = {
                'signature': serialization['signature'],
                'encryption': serialization['encryption'],
                'exchange': serialization['exchange']
            }
        except (TypeError, KeyError) as e:
            raise TypeError(
                'serialization must be compatible with _serialize.'
            ) from e
            
        return cls(keys=keys, guid=guid)
    
    @classmethod
    def new_secret(cls):
        ''' Placeholder method to create new symmetric secret.
        '''
        return super().new_secret(key=bytes(32), seed=None)
        
    def _sign(self, data):
        ''' Placeholder signing method.
        
        Data must be bytes-like. Private key should be a dictionary 
        formatted with all necessary components for a private key (?).
        '''
        return _dummy_signature
    
    @classmethod
    def _verify(cls, public, signature, data):
        ''' Verifies an author's signature against bites. Errors out if 
        unsuccessful. Returns True if successful.
        
        Data must be bytes-like. public_key should be a dictionary 
        formatted with all necessary components for a public key (?).
        Signature must be bytes-like.
        '''
        cls._typecheck_2ndparty(public)
        return True
        
    def _encrypt_asym(self, public, data):
        ''' Placeholder asymmetric encryptor.
        
        Data should be bytes-like. Public key should be a dictionary 
        formatted with all necessary components for a public key.
        '''
        self._typecheck_2ndparty(public)
        return _dummy_asym
        
    def _decrypt_asym(self, data):
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
    def _decrypt(cls, secret, data):
        ''' Placeholder symmetric decryptor.
        
        Data should be bytes-like. Key should be bytes-like.
        '''
        return b'[[ PLACEHOLDER DECRYPTED SYMMETRIC MESSAGE. Hello world! ]]'
        
    @classmethod
    def _encrypt(cls, secret, data):
        ''' Placeholder symmetric encryptor.
        
        Data should be bytes-like. Key should be bytes-like.
        '''
        return b'[[ PLACEHOLDER ENCRYPTED SYMMETRIC MESSAGE. Hello, world? ]]'
    
    def _derive_shared(self, partner):
        ''' Derive a shared secret with the partner.
        '''
        self._typecheck_2ndparty(partner)
        return b'[[ Placeholder shared secret ]]'
        
    @classmethod
    def _mac(cls, key, data):
        ''' Generate a MAC for data using key.
        '''
        return _dummy_mac
        
    @classmethod
    def _verify_mac(cls, key, mac, data):
        return True
    
        
class ThirdParty0(_ThirdPartyBase):
    _ciphersuite = 0
    # Note that, since this classmethod is from a different class, the
    # cls passed internally will be FirstParty0, NOT ThirdParty0.
    _verify = FirstParty0._verify
        
        
class SecondParty1(_SecondPartyBase, _IdentityBase): 
    _ciphersuite = 1  
        
    @classmethod
    def _pack_keys(cls, keys):
        packkeys = {
            'signature': int.to_bytes(keys['signature'].n, length=512, byteorder='big'),
            'encryption': int.to_bytes(keys['encryption'].n, length=512, byteorder='big'),
            'exchange': keys['exchange'].public,
        }
        return packkeys


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
class FirstParty1(_FirstPartyBase, _IdentityBase):
    ''' ... Hmmm
    '''
    _ciphersuite = 1
    _2PID = SecondParty1
        
    # Well it's not exactly repeating yourself, though it does mean there
    # are sorta two ways to perform decryption. Best practice = always decrypt
    # using the author's SecondParty
        
    @classmethod
    def _generate_second_party(cls, keys, address_algo):
        pubkeys = {
            'signature': keys['signature'].publickey(),
            'encryption': keys['encryption'].publickey(),
            'exchange': keys['exchange'].get_public()
        } 
        del keys
        return cls._2PID.from_keys(keys=pubkeys, address_algo=address_algo)
        
    @classmethod
    def _generate_keys(cls):
        keys = {}
        keys['signature'] = RSA.generate(4096)
        keys['encryption'] = RSA.generate(4096)
        keys['exchange'] = ECDHPrivate()
        return keys
        
    def _serialize(self):
        return {
            'guid': bytes(self.guid),
            'signature': self._signature_key.exportKey(format='DER'),
            'encryption': self._encryption_key.exportKey(format='DER'),
            'exchange': bytes(self._exchange_key.private)
        }
        
    @classmethod
    def _from_serialized(cls, serialization):
        try:
            guid = Guid.from_bytes(serialization['guid'])
            keys = {
                'signature': RSA.import_key(serialization['signature']),
                'encryption': RSA.import_key(serialization['encryption']),
                'exchange': ECDHPrivate.load(serialization['exchange'])
            }
        except (TypeError, KeyError) as e:
            raise TypeError(
                'serialization must be compatible with _serialize.'
            ) from e
            
        return cls(keys=keys, guid=guid)
    
    @classmethod
    def new_secret(cls):
        ''' Returns a new secure Secret().
        '''
        key = get_random_bytes(32)
        nonce = get_random_bytes(16)
        return super().new_secret(key=key, seed=nonce)
        
    @classmethod
    def _encrypt(cls, secret, data):
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
        
    @classmethod
    def _decrypt(cls, secret, data):
        ''' Symmetric decryptor.
        
        Handle multiple ciphersuites by having a SecondParty for
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
        
    def _sign(self, data):
        ''' Signing method.
        '''
        h = _FrozenSHA512(data)
        signer = PSS.new(
            self._signature_key, 
            mask_func=_PSS_MGF, 
            salt_bytes=_PSS_SALT_LENGTH
        )
        return signer.sign(h)
       
    @classmethod
    def _verify(cls, public, signature, data):
        ''' Verifies an author's signature against bites. Errors out if 
        unsuccessful. Returns True if successful.
        
        Data must be bytes-like. public_key should be a dictionary 
        formatted with all necessary components for a public key (?).
        Signature must be bytes-like.
        '''
        cls._typecheck_2ndparty(public)
        
        h = _FrozenSHA512(data)
        signer = PSS.new(public._signature_key, mask_func=_PSS_MGF, salt_bytes=_PSS_SALT_LENGTH)
        try:
            signer.verify(h, signature)
        except ValueError as e:
            raise SecurityError('Failed to verify signature.') from e
            
        return True
        
    def _encrypt_asym(self, public, data):
        ''' Placeholder asymmetric encryptor.
        
        Data should be bytes-like. Public key should be a dictionary 
        formatted with all necessary components for a public key.
        '''
        self._typecheck_2ndparty(public)
        cipher = OAEP.new(public._encryption_key)
        return cipher.encrypt(data)
        
    def _decrypt_asym(self, data):
        ''' Placeholder asymmetric decryptor.
        '''
        cipher = OAEP.new(self._encryption_key)
        plaintext = cipher.decrypt(data)
        del cipher
        return plaintext
    
    def _derive_shared(self, partner):
        ''' Derive a shared secret with the partner.
        '''
        # Call the donna25519 exchange method and return bytes
        ecdh = self._exchange_key.do_exchange(partner._exchange_key)
        
        # Get both of our addresses and then the bitwise XOR of them both
        my_hash = self.guid.address
        their_hash = partner.guid.address
        salt = bytes([a ^ b for a, b in zip(my_hash, their_hash)])
        
        key = HKDF(
            master = ecdh, 
            key_len = SHA512.digest_size,
            salt = salt,
            hashmod = SHA512
        )
        # Might as well do this immediately, not that it really adds anything
        del ecdh, my_hash, their_hash, salt
        return key
        
    @classmethod
    def _mac(cls, key, data):
        ''' Generate a MAC for data using key.
        '''
        h = HMAC.new(
            key = key,
            msg = data,
            digestmod = SHA512
        )
        d = h.digest()
        # Do this "just in case" to prevent accidental future updates
        del h
        return d
        
    @classmethod
    def _verify_mac(cls, key, mac, data):
        ''' Verify an existing MAC.
        '''
        mac = bytes(mac)
        data = bytes(data)
        
        h = HMAC.new(
            key = key,
            msg = data,
            digestmod = SHA512
        )
        try:
            h.verify(mac)
        except ValueError as e:
            raise SecurityError('Failed to verify MAC.') from e
            
        return True
        
        
class ThirdParty1(_ThirdPartyBase):
    _ciphersuite = 1
    # Note that, since this classmethod is from a different class, the
    # cls passed internally will be FirstParty0, NOT ThirdParty0.
    _verify = FirstParty1._verify