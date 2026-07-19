class OrderedSet:
    def __init__(self):
        self.items = {}
    def add(self, item):
        self.items[item] = None
    def __iter__(self):
        return iter(self.items.keys())
    def __bool__(self):
        return bool(self.items)

class RawFamilyAdapter:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.params = []
        self.joins = OrderedSet()
        self.where_clauses = []
        self._build_query()

    def _build_query(self):
        self.base_table = 'tblFamilies'
        self.table_alias = 'f'
        
        # We always filter out soft-deleted families unless show_history is True
        # Usually Django ORM handles validity_to=None for families automatically if not show_history
        show_history = self.kwargs.get('show_history', False)
        if not show_history:
            self.where_clauses.append(f'{self.table_alias}.ValidityTo IS NULL')

        import re
        normalized_kwargs = {}
        for key, value in self.kwargs.items():
            if key in ['show_history', 'client_mutation_id', 'parentLocation', 'parentLocationLevel', 'parent_location', 'parent_location_level', 'orderBy', 'first', 'last', 'before', 'after', 'offset', 'officer', 'nullAsFalsePoverty', 'null_as_false_poverty']:
                normalized_kwargs[key] = value
                continue
            
            if isinstance(value, str):
                value = value.strip()
            
            # GraphQL converts keys to camelCase (e.g. headInsuree_OtherNames_Icontains)
            # We want to convert this to snake case, but keep the exact path.
            # Convert 'headInsuree' to 'head_insuree'
            # Convert 'ChfId' to 'chf_id'
            snake_key = re.sub(r'(?<!^)(?=[A-Z])', '_', key).lower()
            
            suffix = ""
            for s in ['__gt', '__gte', '__lt', '__lte', '__icontains', '__istartswith', '__isnull']:
                if snake_key.endswith(s):
                    suffix = s
                    snake_key = snake_key[:-len(s)]
                    break
                    
            snake_key = re.sub(r'_+', '_', snake_key)
            
            if snake_key.startswith('head_insuree_') and not snake_key.startswith('head_insuree__'):
                snake_key = snake_key.replace('head_insuree_', 'head_insuree__', 1)
            if snake_key.startswith('members_') and not snake_key.startswith('members__'):
                snake_key = snake_key.replace('members_', 'members__', 1)
            
            # If the filter targets head_insuree__family__confirmation_type__code
            snake_key = snake_key.replace('family_confirmation_type_code', 'family__confirmation_type__code')
            
            snake_key += suffix
            normalized_kwargs[snake_key] = value

        kwargs = normalized_kwargs
        
        # 1. Location Logic
        parent_location = kwargs.get('parentLocation') or kwargs.get('parent_location')
        if parent_location is not None:
            parent_location_level = kwargs.get('parentLocationLevel') or kwargs.get('parent_location_level')
            if parent_location_level is not None:
                from location.apps import LocationConfig
                depth = len(LocationConfig.location_types) - parent_location_level - 1
                
                self.joins.add('LEFT JOIN tblLocations loc0 WITH (NOLOCK) ON f.LocationId = loc0.LocationId AND loc0.ValidityTo IS NULL')
                loc_alias = 'loc0'
                
                for i in range(depth):
                    next_loc = f'loc{i+1}'
                    self.joins.add(f'LEFT JOIN tblLocations {next_loc} WITH (NOLOCK) ON {loc_alias}.ParentLocationId = {next_loc}.LocationId AND {next_loc}.ValidityTo IS NULL')
                    loc_alias = next_loc
                    
                self.where_clauses.append(f'{loc_alias}.LocationUUID = %s')
                self.params.append(parent_location)

        # 2. Poverty Logic
        null_as_false_poverty = kwargs.get('nullAsFalsePoverty') or kwargs.get('null_as_false_poverty')
        if null_as_false_poverty is not None:
            if str(null_as_false_poverty).lower() == 'true':
                self.where_clauses.append("f.Poverty = 1")
            else:
                self.where_clauses.append("(f.Poverty = 0 OR f.Poverty IS NULL)")

        # 3. Officer Logic
        officer = kwargs.get('officer')
        if officer:
            self.where_clauses.append("f.FamilyID IN (SELECT p.FamilyID FROM tblPolicy p WITH (NOLOCK) INNER JOIN tblOfficer o WITH (NOLOCK) ON p.OfficerID = o.OfficerID WHERE o.OfficerUUID = %s AND p.ValidityTo IS NULL AND o.ValidityTo IS NULL)")
            self.params.append(officer)

        # 4. Joins for Head and Members
        if any(k.startswith('head_insuree__') for k in kwargs):
            self.joins.add('LEFT JOIN tblInsuree hi WITH (NOLOCK) ON f.InsureeID = hi.InsureeID AND hi.ValidityTo IS NULL')
        
        if any(k.startswith('members__') for k in kwargs):
            self.joins.add('LEFT JOIN tblInsuree m WITH (NOLOCK) ON f.FamilyID = m.FamilyID AND m.ValidityTo IS NULL')

        prefix_alias_map = {
            'head_insuree__': 'hi',
            'members__': 'm',
        }
        
        alias_column_overrides = {
            'hi': {'uuid': 'InsureeUUID'},
            'm': {'uuid': 'InsureeUUID'}
        }
        
        # Base column map (applies mostly to tblFamilies or defaults for Insuree if not overridden)
        column_map = {
            'uuid': 'FamilyUUID', 'chf_id': 'CHFID', 'chfid': 'CHFID',
            'last_name': 'LastName', 'other_names': 'OtherNames',
            'nin': 'NIN', 'passport': 'passport', 'phone': 'Phone',
            'gender__code': 'Gender', 'gender': 'Gender',
            'marital': 'Marital', 'dob': 'DOB',
            'validity_from': 'ValidityFrom', 'validityFrom': 'ValidityFrom',
            'status': 'InsureeStatus'
        }

        # Handling head_insuree__family__confirmation_type__code
        # It's an edge case, but we can resolve it directly on `f` since `hi.FamilyID = f.FamilyID`.
        # However, members__family__confirmation_type__code is also just `f.ConfirmationType`.
        
        for key, value in kwargs.items():
            if key in ['show_history', 'client_mutation_id', 'parentLocation', 'parentLocationLevel', 'parent_location', 'parent_location_level', 'orderBy', 'first', 'last', 'before', 'after', 'offset', 'officer', 'nullAsFalsePoverty', 'null_as_false_poverty']:
                continue
            
            operator = '='
            is_null_check = False
            base_key = key
            
            if base_key.endswith('__gt'):
                operator, base_key = '>', base_key[:-4]
            elif base_key.endswith('__gte'):
                operator, base_key = '>=', base_key[:-5]
            elif base_key.endswith('__lt'):
                operator, base_key = '<', base_key[:-4]
            elif base_key.endswith('__lte'):
                operator, base_key = '<=', base_key[:-5]
            elif base_key.endswith('__icontains'):
                # OPTIMIZATION
                operator, base_key = 'LIKE', base_key[:-11]
                value = f'{value}%'
            elif base_key.endswith('__istartswith'):
                operator, base_key = 'LIKE', base_key[:-13]
                value = f'{value}%'
            elif base_key.endswith('__isnull'):
                is_null_check = True
                operator = 'IS NULL' if value else 'IS NOT NULL'
                base_key = base_key[:-8]
            
            col_prefix = f'{self.table_alias}.'
            real_key = base_key
            matched_alias = None
            
            for prefix, join_alias in prefix_alias_map.items():
                if base_key.startswith(prefix):
                    col_prefix = f'{join_alias}.'
                    matched_alias = join_alias
                    real_key = base_key[len(prefix):]
                    break
            
            col_name = None
            
            if matched_alias and real_key in alias_column_overrides.get(matched_alias, {}):
                col_name = alias_column_overrides[matched_alias][real_key]
            elif real_key == 'family__confirmation_type__code':
                # Map to f.ConfirmationType
                col_prefix = 'f.'
                col_name = 'ConfirmationType'
            else:
                col_name = column_map.get(real_key, real_key)
            
            if is_null_check:
                self.where_clauses.append(f'{col_prefix}{col_name} {operator}')
            else:
                self.where_clauses.append(f'{col_prefix}{col_name} {operator} %s')
                self.params.append(value)

    def get_id_query(self):
        query = f'SELECT DISTINCT {self.table_alias}.FamilyID FROM {self.base_table} {self.table_alias} WITH (NOLOCK) '
        if self.joins:
            query += ' '.join(self.joins) + ' '
        
        if self.where_clauses:
            query += 'WHERE ' + ' AND '.join(self.where_clauses) + ' '
            
        return query, self.params
